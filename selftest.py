"""Cargo-free self-test: proves the mechanics of #5 (compounding accepted
patches into the baseline) and #6 (the structured event log) deterministically,
with a mock Target — no cargo build required."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from aro.engine import run_backtest
from aro.events import EventLog
from aro.generator import PlannedGenerator
from aro.store import Memory
from aro.types import Edit, Metrics, Verdict

FAST = "src/opt.rs"  # an edit on this path makes the mock bench ~5% faster


class MockTarget:
    """In-memory target. bench() gets faster for each FAST edit applied to a
    worktree, so a candidate carrying it is 'accepted' and compounding triggers."""
    name = "mock"

    def __init__(self):
        self._wt = {}          # live worktree -> list of applied edit paths
        self.apply_log = []    # permanent (work, path) history
        self._tick = 0

    def objectives(self):
        return []

    def make_worktree(self, tag):
        self._tick += 1
        p = f"/tmp/mock-wt-{tag}-{self._tick}"
        self._wt[p] = []
        return p

    def remove_worktree(self, work):
        self._wt.pop(work, None)

    def apply(self, patch, work):
        for e in patch.edits:
            self._wt.setdefault(work, []).append(e.path)
            self.apply_log.append((work, e.path))

    def build(self, work):
        pass

    def test(self, work):
        pass

    def differential(self, work, baseline):
        return True

    def bench(self, work):
        n_fast = self._wt.get(work, []).count(FAST)
        base = 100.0 * (0.95 ** n_fast)        # each FAST edit shaves ~5%
        self._tick += 1
        jit = ((self._tick % 5) - 2) * 0.01    # tiny deterministic jitter
        m = Metrics()
        m.put("metric/x", [base + jit, base, base - jit])
        return m


def run():
    plan = [
        ("opt", "apply the fast edit", [Edit(FAST, "x", "y")]),
        ("ctrl", "noop control on top of the advanced baseline", []),
    ]
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        target = MockTarget()
        memory = Memory(d)
        events = EventLog(d / "events.jsonl", also_console=False)
        report = run_backtest(target, PlannedGenerator(plan), memory,
                              rounds=2, candidates_per_round=1,
                              aa_runs=2, ab_pairs=3, baseline_ref="HEAD",
                              events=events)

        verdicts = {c.id: o.verdict for c, o in report.outcomes}
        ev = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]
        ev_types = [e["event"] for e in ev]
        fast_applies = sum(1 for _, p in target.apply_log if p == FAST)
        advanced = [e for e in ev if e["event"] == "baseline_advanced"]

    # --- #5: compounding -----------------------------------------------------
    assert verdicts.get("opt-r0") == Verdict.ACCEPTED, verdicts
    assert report.pareto == ["opt-r0"], report.pareto
    assert advanced and advanced[0]["by"] == "opt-r0", advanced
    # 3 FAST applies = round-0 candidate + baseline advance + round-1 base_patch.
    # If compounding were broken, round 1 would not re-apply it -> only 1.
    assert fast_applies == 3, f"expected 3 FAST applies (compounded), got {fast_applies}"
    assert verdicts.get("ctrl-r1") == Verdict.WITHIN_NOISE, verdicts
    print(f"#5 OK: opt-r0 accepted -> baseline advanced -> round 1 ran on top "
          f"(FAST applied {fast_applies}x), ctrl-r1 within-noise")

    # --- #6: event log -------------------------------------------------------
    for required in ["run_started", "baseline_built", "floors_calibrated",
                     "round_started", "candidate_proposed", "gate",
                     "candidate_verdict", "baseline_advanced", "run_finished"]:
        assert required in ev_types, f"missing event {required}"
    gates = {e.get("gate") for e in ev if e["event"] == "gate"}
    assert {"guard", "apply", "build", "test", "differential", "significance"} <= gates, gates
    assert all("seq" in e and "ts" in e and "elapsed_s" in e for e in ev)
    print(f"#6 OK: {len(ev)} events, all gates traced {sorted(gates)}")

    # --- #7: agenda — the forward-looking memory behind the reflect loop -----
    with tempfile.TemporaryDirectory() as d2:
        m = Memory(Path(d2))
        added = m.add_directions([
            {"direction": "direction one", "rationale": "because A",
             "source": "reflect-r0", "round": 0},
            {"direction": "Direction One ", "rationale": "dup (normalized away)",
             "source": "x", "round": 0},
            {"direction": "direction two", "rationale": "because B",
             "source": "reflect-r0", "round": 0},
        ])
        assert [a.id for a in added] == ["d1", "d2"], [a.id for a in added]   # dup dropped
        assert len(m.open_directions()) == 2, m.open_directions()
        m.resolve_direction("d1", "dropped")
        assert [d.id for d in m.open_directions()] == ["d2"]
        assert "open agenda" in m.summary()
        reloaded = Memory(Path(d2))   # persisted across reopen, status survives
        assert len(reloaded.directions) == 2
        assert [d.id for d in reloaded.open_directions()] == ["d2"]
    print("#7 OK: agenda add/dedup/resolve/persist + surfaced in summary")

    # --- #8: regression gate parser (N_pre) ----------------------------------
    from aro.target import _count_passed
    assert _count_passed("test result: ok. 12 passed; 0 failed; 0 ignored") == 12
    assert _count_passed("test result: ok. 5 passed; 0 failed\n"
                         "test result: ok. 3 passed; 0 failed") == 8
    assert _count_passed("compiling... no tests ran") is None
    print("#8 OK: _count_passed sums cargo test totals (regression N_pre)")

    # --- #9: thin Ralph driver's block-format parser -------------------------
    from aro.generator import parse_response
    hyp, edits = parse_response(
        "noise\n@@HYPOTHESIS@@ hoist d*x*y\n@@FILE@@ src/a.rs\n"
        "@@SEARCH@@\nlet c = a*b;\n@@REPLACE@@\nlet c = ab;\n@@END@@\ntrailing")
    assert hyp == "hoist d*x*y" and len(edits) == 1 and edits[0].path == "src/a.rs"
    assert edits[0].search == "let c = a*b;" and edits[0].replace == "let c = ab;"
    assert parse_response("no blocks here") is None
    print("#9 OK: Ralph block-format parser (parse_response)")

    # --- #10: region guard enforced  +  #11: direction-aware judge -----------
    from aro.guard import screen as _screen
    from aro.eval import _judge_metric
    from aro.types import Patch as _P, Edit as _E
    assert _screen(_P([_E("src/lib.rs", "a", "b")]), ["src/lib.rs"]) is None
    assert _screen(_P([_E("src/other.rs", "a", "b")]),
                   ["src/lib.rs"]) is not None                             # outside region
    assert _screen(_P([_E("pkg/sub/x.rs", "a", "b")]), ["pkg"]) is None    # under dir region
    assert _judge_metric(-2.0, -3.0, -1.0, 0.5, True) == (True, False)     # minimize win
    assert _judge_metric(+2.0, 1.0, 3.0, 0.5, False) == (True, False)      # maximize win
    assert _judge_metric(+2.0, 1.0, 3.0, 0.5, True) == (False, True)       # minimize regress
    assert _judge_metric(-0.2, -0.6, 0.2, 0.5, True) == (False, False)     # within noise
    print("#10/#11 OK: region guard enforced + direction-aware judge (min/maximize)")

    # --- #12: resume rebuilds the accepted patch from memory -----------------
    with tempfile.TemporaryDirectory() as d3:
        from aro.types import Candidate as _C, Patch as _PP, EvalOutcome as _EO, Verdict as _V
        mem = Memory(Path(d3))
        mem.record(_C(id="opt-r0", hypothesis="x", patch=_PP([_E("src/lib.rs", "old", "new")])),
                   _EO("opt-r0", _V.ACCEPTED))
        reb = Memory(Path(d3)).accepted_edits()   # fresh reopen → rebuild from disk
        assert len(reb) == 1 and reb[0].path == "src/lib.rs", reb
        assert reb[0].search == "old" and reb[0].replace == "new"
    print("#12 OK: resume rebuilds accepted patch from pareto + patches/")
    print("SELFTEST PASSED")


if __name__ == "__main__":
    run()
