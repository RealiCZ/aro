"""Hermetic tests for zero-candidate liveness: down vs dry classification and
factory escalation (T29)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from aro import attempt as atmod
from aro import engine as engmod
from aro import spec as specmod
from aro.events import EventLog
from aro.types import NoiseFloors, Report


def _empty_report(target="livetest"):
    rep = Report(target=target, baseline_ref="HEAD", rounds=1, floors=NoiseFloors(),
                 outcomes=[])
    rep.folded_edits = []
    return rep


def _fn_name_from_target(target) -> str:
    spec = getattr(target, "spec", target)
    ctx = getattr(spec, "context", None) or {}
    anchors = ctx.get("anchors") or []
    if anchors and isinstance(anchors[0], (list, tuple)) and len(anchors[0]) >= 2:
        return anchors[0][1]
    notes = (getattr(spec, "constraints", None) or {}).get("notes") or ""
    if "`" in notes:
        return notes.split("`")[1]
    return "?"


def case_44():
    # --- classification pure ---
    ce = atmod._classify_generator_errors
    assert ce([]) == "down"
    assert ce([{"stage": "claude", "detail": "exited 1"}]) == "down"
    assert ce([{"stage": "worktree", "detail": "fail"}]) == "down"
    assert ce([{"stage": "diff", "detail": "agent made no usable .rs edits"}]) == "dry"
    assert ce([{"stage": "parse", "detail": "no parseable block patch"}]) == "dry"
    # majority dry
    assert ce([{"stage": "diff"}, {"stage": "diff"}, {"stage": "claude"}]) == "dry"
    # majority down
    assert ce([{"stage": "claude"}, {"stage": "claude"}, {"stage": "diff"}]) == "down"
    # tie → down (conservative)
    assert ce([{"stage": "diff"}, {"stage": "claude"}]) == "down"
    assert ce([{"stage": "diff"}, {"stage": "parse"},
               {"stage": "claude"}, {"stage": "codex"}]) == "down"  # 2-2 tie

    # hard-down reason is byte-stable
    assert atmod._hard_down_reason() == (
        "generator hard-down: 3 consecutive zero-candidate attempts (see "
        "generator_error events for the underlying failure)")
    assert atmod._hard_down_reason().startswith(atmod._GENERATOR_DOWN)

    # streak helpers
    assert atmod._generator_down(["down", "down", "down"])
    assert not atmod._generator_down(["down", "dry", "down"])
    assert atmod._generator_dry(["dry", "dry", "dry"])
    assert not atmod._generator_dry(["dry", "down", "dry"])

    # --- attempt-loop integration (mocked profile / locate / backtest) ---
    ranked_base = [
        ("fa", 12.0, "ours::fa"),
        ("fb", 11.0, "ours::fb"),
        ("fc", 10.0, "ours::fc"),
        ("fd", 9.0, "ours::fd"),
        ("fe", 8.0, "ours::fe"),
        ("ff", 7.0, "ours::ff"),
        ("new_region", 6.0, "ours::new_region"),
    ]

    def fake_profile(spec, top=40, our_token="", extra_edits=None):
        return list(ranked_base)

    def fake_locate(target, pkg, name, symbol=""):
        return [f"src/{name}.rs"]

    def fake_workspace_tokens(target, fallback_pkg=""):
        return {"ours"}

    sp = specmod.from_dict({
        "name": "livetest-t29",
        "target_repo": {"path": "."},
        "metric": "ns",
        "hot_path": {"file": "src/lib.rs", "fn": "fa"},
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "run": {"generator": "agentic", "stop": {"max_rounds": 1, "dry_rounds": 1},
                "aa_runs": 1, "ab_pairs": 1},
    })

    def _run(mode: str, *, probe_factory: bool = True, factory_regions=None,
             max_attempts: int = 12):
        """mode: 'down' | 'dry'. factory_regions=None → no injected hook;
        [] → empty factory; list → regions returned once."""
        attempted = []
        factory_calls = []

        def fake_backtest(target, generator, memory, **kw):
            events = kw.get("events")
            fn = _fn_name_from_target(target)
            attempted.append(fn)
            stage = "claude" if mode == "down" else "diff"
            detail = ("quota exceeded" if mode == "down"
                      else "agent made no usable .rs edits")
            if events is not None:
                events.emit("generator_error", generator="agentic", stage=stage,
                            k=0, detail=detail)
            return _empty_report(sp.name)

        hooks = {}
        if factory_regions is not None:
            def ff(spec_, dry_items):
                factory_calls.append([d.get("name") for d in dry_items])
                return list(factory_regions)
            hooks["frontier_factory"] = ff

        orig = engmod.run_backtest
        engmod.run_backtest = fake_backtest
        try:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                # permtree._DIR is bound at import — redirect writes + no-op
                # record/load so the live ledger tree is never touched.
                with mock.patch("aro.sweep.profile_ranked", fake_profile), \
                     mock.patch("aro.attempt._locate_fn", fake_locate), \
                     mock.patch("aro.attempt._workspace_tokens",
                                fake_workspace_tokens), \
                     mock.patch("aro.attempt.permtree.record",
                                lambda *a, **k: None), \
                     mock.patch("aro.attempt.permtree.load",
                                lambda *a, **k: []), \
                     mock.patch("aro.attempt.permtree.baseline_state",
                                lambda *a, **k: "origin"):
                    ev = EventLog(td / "events.jsonl", also_console=False)
                    _rows, _cum, stop = atmod.attempt(
                        sp, max_attempts=max_attempts, rounds_per_fn=1,
                        min_pct=1.0, top=40, out_dir=td, events=ev,
                        diverge=False, probe_factory=probe_factory,
                        probe_hooks=hooks, ledger_name=f"live-{td.name}")
                parsed = [json.loads(x) for x in
                          (td / "events.jsonl").read_text().splitlines() if x]
        finally:
            engmod.run_backtest = orig
        return attempted, factory_calls, stop, parsed

    # (1) 3× down → hard-down, byte-identical reason, factory never called
    att, fcalls, stop, parsed = _run(
        "down", probe_factory=True, factory_regions=[])
    assert stop == atmod._hard_down_reason(), stop
    assert any(e.get("event") == "attempt_abort" and e.get("reason") == stop
               for e in parsed), parsed[-3:]
    assert fcalls == []
    assert len(att) == 3, att

    # (2) 3× dry + factory returns a region → factory once, sweep continues on it
    att, fcalls, stop, parsed = _run(
        "dry", probe_factory=True,
        factory_regions=[{"name": "new_region", "pct": 20.0, "symbol": ""}],
        max_attempts=6)
    assert len(fcalls) == 1, fcalls
    assert "new_region" in att, att
    # factory-produced region is attempted only after the 3 dry that triggered it
    assert att.index("new_region") >= 3, att
    assert any(e.get("event") == "frontier_dry" for e in parsed), parsed
    assert any(e.get("event") == "frontier_factory" for e in parsed), parsed
    assert not stop.startswith(atmod._GENERATOR_DOWN), stop

    # (3) factory returns nothing → frontier-dry reason
    att, fcalls, stop, parsed = _run(
        "dry", probe_factory=True, factory_regions=[])
    assert len(fcalls) == 1, fcalls
    assert stop == atmod._FRONTIER_DRY, stop
    assert any(e.get("event") == "attempt_abort" and e.get("reason") == stop
               for e in parsed)

    # (4) post-escalation 3× dry → abort, no second factory call
    att, fcalls, stop, parsed = _run(
        "dry", probe_factory=True,
        factory_regions=[{"name": "new_region", "pct": 20.0}],
        max_attempts=12)
    assert len(fcalls) == 1, fcalls  # exactly once
    assert stop == atmod._FRONTIER_DRY, stop
    assert "new_region" in att, att
    assert len(att) >= 6, att

    # (5) factory disabled → distinct no-factory reason, hook never consulted
    att, fcalls, stop, parsed = _run(
        "dry", probe_factory=False, factory_regions=[])
    assert fcalls == []
    assert stop == atmod._FRONTIER_DRY_NO_FACTORY, stop
    assert len(att) == 3, att

    print("#44 OK: liveness down/dry classify + factory escalation "
          "(hard-down msg stable, one-shot factory, three abort reasons)")
