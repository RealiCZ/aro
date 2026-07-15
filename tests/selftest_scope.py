"""T30-B: unlocated → out-of-scope-external (closed) + 3× unlocated close."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from aro import attempt as atmod
from aro import engine as engmod
from aro import frontier as frmod
from aro import permtree as ptmod
from aro import spec as specmod
from aro.events import EventLog
from aro.symbols import _symbol_crate_tokens
from aro.types import NoiseFloors, Report


def _empty_report(target="scope-demo"):
    rep = Report(target=target, baseline_ref="HEAD", rounds=1, floors=NoiseFloors(),
                 outcomes=[])
    rep.folded_edits = []
    return rep


def case_46():
    print("=== case 46: out-of-scope-external + unlocated counter ===")

    # --- unit: crate-token extractor + classification helpers ---
    assert _symbol_crate_tokens("revm::interpreter::init_with_context") == ["revm",
                                                                            "interpreter"]
    assert _symbol_crate_tokens("pkg::macro_gen") == ["pkg"]
    assert _symbol_crate_tokens("ghost_fn") == []
    assert _symbol_crate_tokens("") == []
    assert _symbol_crate_tokens(
        "<revm::Journal as mega_evm::Tr>::inspect") == ["revm", "mega_evm"]

    class _T:
        def __init__(self, members=None):
            self.repo = Path("/tmp/fake-repo-scope")
            self._ws_members = list(members or [])

        def pkg_dir(self, work, pkg):
            return Path(work) / pkg

    # foreign-token miss (only non-member crates) → immediate oos
    with mock.patch.object(frmod, "_workspace_tokens",
                           return_value={"mega_evm", "mega_evm_core"}):
        assert frmod._classify_locate_miss(
            _T(["mega-evm"]), "mega-evm", "init_with_context",
            symbol="revm::interpreter::init_with_context") == "out-of-scope-external"
        assert frmod._classify_locate_miss(
            _T(["mega-evm"]), "mega-evm", "foo",
            symbol="alloy_primitives::bits::foo") == "out-of-scope-external"

    # target-crate-token miss with roots ready → stays unlocated (strike 1), NOT oos
    with mock.patch.object(frmod, "_workspace_tokens", return_value={"pkg", "mega_evm"}):
        assert frmod._classify_locate_miss(
            _T(["pkg"]), "pkg", "macro_gen",
            symbol="pkg::opcodes::macro_gen") == "unlocated"
        # trait Self is foreign but Trait crate is ours → still patience
        assert frmod._classify_locate_miss(
            _T(["mega-evm"]), "mega-evm", "inspect",
            symbol="<revm::Journal as mega_evm::Tr>::inspect") == "unlocated"

    # tokenless miss → counter path (unlocated)
    with mock.patch.object(frmod, "_workspace_tokens", return_value={"pkg"}):
        assert frmod._classify_locate_miss(
            _T(["pkg"]), "pkg", "ghost", symbol="ghost") == "unlocated"
        assert frmod._classify_locate_miss(
            _T(["pkg"]), "pkg", "ghost", symbol="") == "unlocated"

    rows = [
        {"workload": "w", "fn": "a", "verdict": "unlocated"},
        {"workload": "w", "fn": "a", "verdict": "unlocated"},
        {"workload": "w", "fn": "b", "verdict": "out-of-scope-external"},
        {"workload": "w", "fn": "c", "verdict": "unlocated"},
        {"workload": "other", "fn": "b", "verdict": "accepted"},
    ]
    assert frmod._unlocated_count(rows, "a", "w") == 2
    assert frmod._unlocated_count(rows, "b", "w") == 0
    assert frmod._closed_out_of_scope(rows, "w") == {"b"}
    assert "out-of-scope-external" in ptmod._CLOSED_VERDICTS
    assert "out-of-scope-external" not in ptmod._OPEN_VERDICTS
    print("#46a OK: foreign→oos; target/tokenless→unlocated; counter/closed-set helpers")

    sp = specmod.from_dict({
        "name": "scope-demo",
        "target_repo": {"path": "."},
        "metric": "ns",
        "hot_path": {"file": "src/lib.rs", "fn": "init_with_context"},
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "pkg"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "run": {"generator": "agentic", "stop": {"max_rounds": 1, "dry_rounds": 1},
                "aa_runs": 1, "ab_pairs": 1},
    })

    def fake_workspace_tokens(target, fallback_pkg=""):
        return {"pkg"}

    def fake_backtest(target, generator, memory, **kw):
        return _empty_report(sp.name)

    # --- foreign-token locate miss → oos closed; second run frontier skips without attempt ---
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ledger = f"scope-{td.name}"

        def fake_profile(*a, **k):
            # Primary crate is foreign (revm) so real _classify_locate_miss → oos.
            # Leaf still contains substring "pkg" so ownership queues it as ours
            # (campaign shape: external credited to us via a loose token match).
            return [("init_with_context", 12.0,
                     "revm::interpreter::init_with_context_for_pkg")]

        with mock.patch.object(ptmod, "_DIR", td / "permtree"), \
             mock.patch("aro.sweep.profile_ranked", fake_profile), \
             mock.patch("aro.attempt._locate_fn", lambda *a, **k: []), \
             mock.patch("aro.attempt._workspace_tokens", fake_workspace_tokens), \
             mock.patch.object(engmod, "run_backtest", fake_backtest):
            # Run 1
            (td / "r1").mkdir()
            ev1 = EventLog(td / "r1" / "events.jsonl", also_console=False)
            rows1, _cum, _stop = atmod.attempt(
                sp, max_attempts=3, rounds_per_fn=1, min_pct=1.0, top=40,
                out_dir=td / "r1", events=ev1, diverge=False,
                ledger_name=ledger)
            parsed1 = [json.loads(x) for x in
                       (td / "r1" / "events.jsonl").read_text().splitlines() if x]
            oos_skips = [e for e in parsed1
                         if e.get("event") == "attempt_skipped"
                         and e.get("fn") == "init_with_context"]
            assert oos_skips, parsed1
            assert oos_skips[0]["reason"] == "out of editable scope (external)", \
                oos_skips[0]
            assert any(r.get("verdict") == "out-of-scope-external"
                       and r.get("name") == "init_with_context" for r in rows1), rows1
            assert any(r.get("fn") == "init_with_context"
                       and r.get("verdict") == "out-of-scope-external"
                       for r in ptmod.load(ledger))

            # Run 2: closed → no attempt_skipped re-emit, not on frontier
            (td / "r2").mkdir()
            ev2 = EventLog(td / "r2" / "events.jsonl", also_console=False)
            rows2, _cum2, _stop2 = atmod.attempt(
                sp, max_attempts=3, rounds_per_fn=1, min_pct=1.0, top=40,
                out_dir=td / "r2", events=ev2, diverge=False,
                ledger_name=ledger)
            parsed2 = [json.loads(x) for x in
                       (td / "r2" / "events.jsonl").read_text().splitlines() if x]
            skips2 = [e for e in parsed2 if e.get("event") == "attempt_skipped"
                      and e.get("fn") == "init_with_context"]
            assert skips2 == [], f"second run must not re-skip closed oos: {skips2}"
            fronts = [e for e in parsed2 if e.get("event") == "attempt_frontier"]
            assert fronts and "init_with_context" not in (fronts[0].get("fns") or []), \
                fronts
            assert not any(r.get("name") == "init_with_context" for r in rows2), rows2
        print("#46b OK: foreign-token miss → oos closed; second run frontier skips")

    # --- target-crate-token miss → unlocated (strike 1), NOT oos ---
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ledger = f"tgt-{td.name}"

        def fake_profile_t(*a, **k):
            return [("macro_gen", 8.0, "pkg::opcodes::macro_gen")]

        with mock.patch.object(ptmod, "_DIR", td / "permtree"), \
             mock.patch("aro.sweep.profile_ranked", fake_profile_t), \
             mock.patch("aro.attempt._locate_fn", lambda *a, **k: []), \
             mock.patch("aro.attempt._workspace_tokens", fake_workspace_tokens), \
             mock.patch.object(engmod, "run_backtest", fake_backtest):
            out = td / "r"
            out.mkdir()
            ev = EventLog(out / "events.jsonl", also_console=False)
            rws, _, _ = atmod.attempt(
                sp, max_attempts=2, rounds_per_fn=1, min_pct=1.0,
                top=40, out_dir=out, events=ev, diverge=False,
                ledger_name=ledger)
            assert rws and rws[0]["verdict"] == "unlocated", rws
            assert rws[0]["name"] == "macro_gen"
            parsed = [json.loads(x) for x in
                      (out / "events.jsonl").read_text().splitlines() if x]
            sk = [e for e in parsed if e.get("event") == "attempt_skipped"]
            assert sk and sk[0]["reason"] == "source not located", sk
            assert all(r.get("verdict") != "out-of-scope-external"
                       for r in ptmod.load(ledger)), ptmod.load(ledger)
        print("#46b2 OK: target-crate-token miss stays unlocated (strike 1)")

    # --- 3× ambiguous/tokenless unlocated → closes as out-of-scope-external ---
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ledger = f"uloc-{td.name}"

        def fake_profile_u(*a, **k):
            # bare leaf: no crate token → counter path; "pkg" substring keeps it ours
            return [("ghost_fn", 5.0, "ghost_fn_in_pkg")]

        with mock.patch.object(ptmod, "_DIR", td / "permtree"), \
             mock.patch("aro.sweep.profile_ranked", fake_profile_u), \
             mock.patch("aro.attempt._locate_fn", lambda *a, **k: []), \
             mock.patch("aro.attempt._workspace_tokens", fake_workspace_tokens), \
             mock.patch.object(engmod, "run_backtest", fake_backtest):
            verdicts, reasons = [], []
            for i in range(3):
                out = td / f"u{i}"
                out.mkdir()
                ev = EventLog(out / "events.jsonl", also_console=False)
                rws, _, _ = atmod.attempt(
                    sp, max_attempts=2, rounds_per_fn=1, min_pct=1.0,
                    top=40, out_dir=out, events=ev, diverge=False,
                    ledger_name=ledger)
                verdicts.append(rws[0]["verdict"] if rws else None)
                parsed = [json.loads(x) for x in
                          (out / "events.jsonl").read_text().splitlines() if x]
                sk = [e for e in parsed if e.get("event") == "attempt_skipped"]
                reasons.append(sk[0]["reason"] if sk else None)

            assert verdicts[0] == "unlocated" and reasons[0] == "source not located", \
                (verdicts, reasons)
            assert verdicts[1] == "unlocated" and reasons[1] == "source not located", \
                (verdicts, reasons)
            assert verdicts[2] == "out-of-scope-external", verdicts
            assert reasons[2] == "out of editable scope (external)", reasons
            last = [r for r in ptmod.load(ledger)
                    if r.get("fn") == "ghost_fn"][-1]
            assert last["verdict"] == "out-of-scope-external"
            assert "unlocated 3x" in (last.get("hypothesis") or ""), last
        print("#46c OK: tokenless unlocated ×3 closes as out-of-scope-external")

    # --- single-shot unlocated keeps today's reason ---
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ledger = f"one-{td.name}"
        with mock.patch.object(ptmod, "_DIR", td / "permtree"), \
             mock.patch("aro.sweep.profile_ranked",
                        lambda *a, **k: [("once", 3.0, "once_in_pkg")]), \
             mock.patch("aro.attempt._locate_fn", lambda *a, **k: []), \
             mock.patch("aro.attempt._workspace_tokens", fake_workspace_tokens), \
             mock.patch.object(engmod, "run_backtest", fake_backtest):
            out = td / "r"
            out.mkdir()
            ev = EventLog(out / "events.jsonl", also_console=False)
            rws, _, _ = atmod.attempt(
                sp, max_attempts=2, rounds_per_fn=1, min_pct=1.0,
                top=40, out_dir=out, events=ev, diverge=False,
                ledger_name=ledger)
            assert rws and rws[0]["verdict"] == "unlocated"
            parsed = [json.loads(x) for x in
                      (out / "events.jsonl").read_text().splitlines() if x]
            sk = [e for e in parsed if e.get("event") == "attempt_skipped"]
            assert sk and sk[0]["reason"] == "source not located", sk
        print("#46d OK: single unlocated keeps 'source not located' reason")
    print("case 46 OK")
