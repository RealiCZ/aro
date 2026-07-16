"""T31: generator backend preflight on `aro sweep --attempt`.

Hermetic — no real LLM, network, or cargo. Patches the llmmod.run_llm seam
and stubs the downstream attempt loop.
"""
from __future__ import annotations

import io
import json
import tempfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aro import llm as llmmod
from aro import permtree as permtreemod
from aro import spec as specmod
from aro import sweep as sweepmod
from aro.events import EventLog


def _mini_spec(name="preflight-t31", *, llm_backend="claude",
               critic_backend=None):
    d = {
        "name": name,
        "target_repo": {"path": "."},
        "metric": "ns",
        "hot_path": {"file": "src/lib.rs", "fn": "f"},
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "run": {"generator": "agentic", "stop": {"max_rounds": 1, "dry_rounds": 1},
                "aa_runs": 1, "ab_pairs": 1},
        "llm_backend": llm_backend,
    }
    if critic_backend is not None:
        d["critic_backend"] = critic_backend
    return specmod.from_dict(d)


def _attempt_args(out_dir, *, critic=False):
    return SimpleNamespace(
        spec="t.json",
        out=None,
        min_pct=1.5,
        top=40,
        attempt=True,
        diverge=False,
        critic=critic,
        max_attempts=1,
        rounds_per_fn=1,
        max_tries_per_fn=0,
        dry_rounds=None,
        fanout=None,
        gen_concurrency=8,
        out_dir=str(out_dir),
        prescreen=None,
        exhaustive=None,
        probe_factory=None,
        workloads=0,
    )


def _parse_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def _enter_cli_patches(stack, sp, ptree, run_llm, *, attempt=None,
                       finalize=None, mark_state=None):
    """Enter common hermetic patches for sweep.cli --attempt tests."""
    stack.enter_context(mock.patch.object(specmod, "load", return_value=sp))
    stack.enter_context(
        mock.patch.object(sweepmod.llmmod, "run_llm", run_llm))
    stack.enter_context(mock.patch.object(permtreemod, "_DIR", ptree))
    stack.enter_context(mock.patch("aro.permtree._DIR", ptree))
    stack.enter_context(mock.patch(
        "aro.attempt.attempt",
        attempt or (lambda *a, **k: ([], [], "stub-done"))))
    stack.enter_context(mock.patch(
        "aro.attempt._finalize_run",
        finalize or (lambda *a, **k: None)))
    stack.enter_context(mock.patch("aro.permtree.load", lambda *a, **k: []))
    stack.enter_context(
        mock.patch("aro.permtree.debt_keys", lambda *a, **k: []))
    if mark_state is not None:
        stack.enter_context(
            mock.patch("aro.permtree.mark_state", mark_state))


def case_48():
    """T31: generator preflight fail/pass/empty/critic; zero attempts on fail."""
    print("=== case 48: generator backend preflight (sweep --attempt) ===")

    # --- helper: empty-reply is fail ------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ev = EventLog(td / "events.jsonl", also_console=False)
        be = llmmod.get_backend("claude")

        with mock.patch.object(sweepmod.llmmod, "run_llm",
                               lambda *_a, **_k: ("", 0, 0.0)):
            try:
                sweepmod._preflight_generator(be, ev)
                raise AssertionError("empty reply must SystemExit(1)")
            except SystemExit as se:
                assert se.code == 1, se.code
        rows = _parse_events(td / "events.jsonl")
        assert len(rows) == 1 and rows[0]["event"] == "generator_preflight"
        assert rows[0]["status"] == "fail" and rows[0]["backend"] == "claude"
        assert "empty" in (rows[0].get("detail") or "").lower()
    print("#48a OK: empty reply → fail event + SystemExit(1)")

    # --- helper: LLMError is fail ---------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ev = EventLog(td / "events.jsonl", also_console=False)
        be = llmmod.get_backend("claude")

        def boom(*_a, **_k):
            raise llmmod.LLMError("claude exited 1: dead account")

        with mock.patch.object(sweepmod.llmmod, "run_llm", boom):
            try:
                sweepmod._preflight_generator(be, ev)
                raise AssertionError("LLMError must SystemExit(1)")
            except SystemExit as se:
                assert se.code == 1
        rows = _parse_events(td / "events.jsonl")
        assert rows[0]["status"] == "fail"
        assert "dead account" in rows[0]["detail"]
    print("#48b OK: LLMError → fail + detail")

    # --- helper: OK is pass ---------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ev = EventLog(td / "events.jsonl", also_console=False)
        be = llmmod.get_backend("codex")

        with mock.patch.object(sweepmod.llmmod, "run_llm",
                               lambda *_a, **_k: ("OK", 1, 0.0)):
            sweepmod._preflight_generator(be, ev)  # no raise
        rows = _parse_events(td / "events.jsonl")
        assert rows[0]["status"] == "pass" and rows[0]["backend"] == "codex"
    print("#48c OK: OK reply → pass event")

    # --- critic: distinct name ⇒ two probes; same name ⇒ one ------------------
    calls = []

    def track_ok(prompt, *, backend=None, **_k):
        name = backend.name if hasattr(backend, "name") else backend
        calls.append(name)
        return ("OK", 1, 0.0)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ev = EventLog(td / "events.jsonl", also_console=False)
        gen = llmmod.get_backend("codex")
        crit = llmmod.get_backend("claude")
        with mock.patch.object(sweepmod.llmmod, "run_llm", track_ok):
            sweepmod._preflight_generator(gen, ev)
            if crit.name != gen.name:
                sweepmod._preflight_generator(crit, ev)
        assert calls == ["codex", "claude"], calls
        rows = _parse_events(td / "events.jsonl")
        assert [r["backend"] for r in rows] == ["codex", "claude"]
        assert all(r["status"] == "pass" for r in rows)

    calls.clear()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ev = EventLog(td / "events.jsonl", also_console=False)
        gen = llmmod.get_backend("claude")
        crit = llmmod.get_backend("claude")
        with mock.patch.object(sweepmod.llmmod, "run_llm", track_ok):
            sweepmod._preflight_generator(gen, ev)
            if crit.name != gen.name:
                sweepmod._preflight_generator(crit, ev)
        assert calls == ["claude"], calls
        assert len(_parse_events(td / "events.jsonl")) == 1
    print("#48d OK: distinct critic ⇒ 2 probes; same name ⇒ 1")

    # --- CLI fail path: LLMError → exit 1, fail event, no attempt_*, no pid ---
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out_dir = td / "out"
        ptree = td / "permtree"
        ptree.mkdir()
        sp = _mini_spec("preflight-fail")
        mark_calls = []
        attempt_calls = []

        def boom_cli(*_a, **_k):
            raise llmmod.LLMError("claude exited 1: quota dead")

        def track_mark(*a, **k):
            mark_calls.append((a, k))
            raise AssertionError("mark_state must not run on preflight fail")

        def track_attempt(*a, **k):
            attempt_calls.append(1)
            raise AssertionError("attempt must not run on preflight fail")

        with ExitStack() as stack:
            _enter_cli_patches(stack, sp, ptree, boom_cli,
                               attempt=track_attempt, mark_state=track_mark)
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    sweepmod.cli(_attempt_args(out_dir))
                    raise AssertionError(
                        "cli should SystemExit(1) on preflight fail")
                except SystemExit as se:
                    assert se.code == 1, se.code
            text = buf.getvalue()
            assert "preflight: generator backend 'claude' unavailable" in text
            assert "quota dead" in text

        rows = _parse_events(out_dir / "events.jsonl")
        assert rows, "events.jsonl must record the preflight fail"
        pf = [r for r in rows if r.get("event") == "generator_preflight"]
        assert len(pf) == 1 and pf[0]["status"] == "fail"
        assert pf[0]["backend"] == "claude"
        assert "quota" in pf[0]["detail"]
        assert not any(r.get("event", "").startswith("attempt_") for r in rows)
        assert mark_calls == []
        assert attempt_calls == []
        st_path = ptree / f"{sp.name}.state.json"
        if st_path.exists():
            st = json.loads(st_path.read_text())
            assert st.get("running_pid") in (None, ""), st
    print("#48e OK: CLI fail → exit 1, fail event, no attempt_*, no running_pid")

    # --- CLI pass path: proceeds past preflight; pass event; attempt called ---
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out_dir = td / "out"
        ptree = td / "permtree"
        ptree.mkdir()
        sp = _mini_spec("preflight-pass", llm_backend="codex")
        attempt_calls = []
        mark_pids = []

        def fake_attempt(spec, **kw):
            attempt_calls.append(spec.name)
            return [], [], "stub-done"

        real_mark = permtreemod.mark_state

        def spy_mark(name, **fields):
            if "running_pid" in fields:
                mark_pids.append(fields["running_pid"])
            return real_mark(name, **fields)

        with ExitStack() as stack:
            _enter_cli_patches(stack, sp, ptree,
                               lambda *_a, **_k: ("OK", 1, 0.0),
                               attempt=fake_attempt, mark_state=spy_mark)
            with redirect_stdout(io.StringIO()):
                sweepmod.cli(_attempt_args(out_dir))

        rows = _parse_events(out_dir / "events.jsonl")
        pf = [r for r in rows if r.get("event") == "generator_preflight"]
        assert len(pf) == 1 and pf[0]["status"] == "pass", pf
        assert pf[0]["backend"] == "codex"
        assert attempt_calls == ["preflight-pass"], attempt_calls
        assert mark_pids and mark_pids[0] is not None, mark_pids
    print("#48f OK: CLI pass → pass event, attempt proceeds, mark_state after")

    # --- CLI critic: distinct backends ⇒ two probes ---------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out_dir = td / "out"
        ptree = td / "permtree"
        ptree.mkdir()
        sp = _mini_spec("preflight-critic", llm_backend="codex",
                        critic_backend="claude")
        probe_backends = []

        def track_and_ok(prompt, *, backend=None, **_k):
            name = backend.name if hasattr(backend, "name") else str(backend)
            probe_backends.append(name)
            return ("OK", 1, 0.0)

        with ExitStack() as stack:
            _enter_cli_patches(stack, sp, ptree, track_and_ok)
            with redirect_stdout(io.StringIO()):
                sweepmod.cli(_attempt_args(out_dir, critic=True))

        assert probe_backends == ["codex", "claude"], probe_backends
        pf = [r for r in _parse_events(out_dir / "events.jsonl")
              if r.get("event") == "generator_preflight"]
        assert [r["backend"] for r in pf] == ["codex", "claude"]
        assert all(r["status"] == "pass" for r in pf)
    print("#48g OK: --critic distinct backends ⇒ two preflight probes")

    # same-name critic: one probe
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out_dir = td / "out"
        ptree = td / "permtree"
        ptree.mkdir()
        sp = _mini_spec("preflight-same", llm_backend="claude",
                        critic_backend="claude")
        probe_backends = []

        def track_and_ok(prompt, *, backend=None, **_k):
            name = backend.name if hasattr(backend, "name") else str(backend)
            probe_backends.append(name)
            return ("OK", 1, 0.0)

        with ExitStack() as stack:
            _enter_cli_patches(stack, sp, ptree, track_and_ok)
            with redirect_stdout(io.StringIO()):
                sweepmod.cli(_attempt_args(out_dir, critic=True))

        assert probe_backends == ["claude"], probe_backends
        pf = [r for r in _parse_events(out_dir / "events.jsonl")
              if r.get("event") == "generator_preflight"]
        assert len(pf) == 1
    print("#48h OK: --critic same backend name ⇒ one probe")

    print("#48 OK: generator backend preflight")
