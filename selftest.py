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

    def bench(self, work, scale=1):
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

    # --- #13: 7-slot spec loader normalizes into the driver fields -----------
    from aro import spec as _spec, plan as _plan
    sd = {
        "name": "demo",
        "target_repo": {"path": "/tmp/repo", "baseline_ref": "v1"},
        "hot_path": {"file": "foo/src/x.rs", "fn": "hot"},
        "metric": "tps", "direction": "maximize",
        "benchmark_probe": {"pkg": "foo", "probe": "probes/d.rs", "example": "d",
                            "sample_prefix": "B", "profile": {"spin_secs": 3, "sample_secs": 2}},
        "correctness_oracle": {"build": ["cargo", "build"], "test": ["cargo", "test"],
                               "differential": {"pkg": "foo", "probe": "probes/d_diff.rs",
                                                "example": "d_diff", "prefix": "DIFF"}},
        "constraints": {"editable": ["foo/src/x.rs", "foo/src/y.rs"]},
        "run": {"generator": "ralph", "goal_target": 1000.0,
                "stop": {"max_rounds": 5, "dry_rounds": 1}, "aa_runs": 4, "ab_pairs": 8},
    }
    sp = _spec.from_dict(sd)
    assert sp.baseline_ref == "v1" and sp.build == ["cargo", "build"]
    assert sp.bench == {"probe": "probes/d.rs", "example": "d", "pkg": "foo",
                        "sample_prefix": "B", "metric": "tps"}
    assert sp.profile == {"example": "d", "spin_secs": 3, "sample_secs": 2}
    assert sp.regions == ["foo/src/x.rs", "foo/src/y.rs"]          # from constraints.editable
    assert sp.context == {"file": "foo/src/x.rs", "anchors": [["fn", "hot"]]}
    assert sp.objectives == [{"metric": "tps", "minimize": False}]  # direction=maximize
    assert sp.goal.direction == "maximize" and sp.goal.target == 1000.0
    assert sp.stop.max_rounds == 5 and sp.stop.dry_rounds == 1
    assert sp.generator == "ralph" and sp.aa_runs == 4 and sp.ab_pairs == 8
    # editable default = [hot_path.file] when constraints.editable absent
    sp2 = _spec.from_dict({**sd, "constraints": {}})
    assert sp2.regions == ["foo/src/x.rs"]
    # plan.assemble_spec emits a dict from_dict accepts
    asm = _plan.assemble_spec("p", Path("/tmp/r"), "HEAD", "foo",
                              {"hot_path": {"file": "foo/src/x.rs", "fn": "h"},
                               "metric": "ns", "direction": "minimize", "has_diff": True})
    sp3 = _spec.from_dict(asm)
    assert sp3.bench["pkg"] == "foo" and sp3.differential["example"] == "p_diff"
    print("#13 OK: 7-slot loader normalizes + plan.assemble_spec round-trips")

    # --- #14: memory best-delta is direction-aware (maximize) ----------------
    with tempfile.TemporaryDirectory() as d4:
        mm = Memory(Path(d4))
        # a maximize win (+5% improved) must beat a guard metric (-1%, not improved)
        mm.rows = [{"id": "c1", "metrics": [
            {"metric": "tps", "delta_pct": 5.0, "improved": True},
            {"metric": "allocs", "delta_pct": -1.0, "improved": False}]}]
        assert mm._best_delta("c1") == ("tps", 5.0), mm._best_delta("c1")
        # nothing improved → report the primary objective (first metric)
        mm.rows = [{"id": "c2", "metrics": [
            {"metric": "tps", "delta_pct": 0.2, "improved": False},
            {"metric": "allocs", "delta_pct": -3.0, "improved": False}]}]
        assert mm._best_delta("c2") == ("tps", 0.2), mm._best_delta("c2")
    print("#14 OK: best-delta is direction-aware (maximize win not mislabeled)")

    # --- #15: noise_limited flag — same Δ, floor decides limited vs win ------
    from aro import eval as _evalmod
    from aro.types import NoiseFloors as _NF

    class _StubT:
        def bench(self, work, scale=1):
            m = Metrics()
            # candidate (FAST applied to this worktree) is a clean -3%
            v = 97.0 if FAST in self._fast.get(work, []) else 100.0
            m.put("m", [v, v, v]); return m
        _fast = {"cand": [FAST], "base": []}
    from aro.types import Objective as _Obj
    st = _StubT()
    objs = [_Obj("m", True)]
    obj_min = {"m": True}
    hi = _NF(); hi.put("m", 12.0)     # floor 12% > 3% signal
    lo = _NF(); lo.put("m", 1.5)      # floor 1.5% < 3% signal
    dh, ah = _evalmod._significance(st, "base", "cand", 4, 1, obj_min, objs, hi)
    dl, al = _evalmod._significance(st, "base", "cand", 4, 8, obj_min, objs, lo)
    assert ah["noise_limited"] and not ah["improved"], ah   # CI excludes 0 but |Δ|<floor
    assert al["improved"] and not al["noise_limited"], al   # same Δ clears the lower floor
    assert dh[0].bench_scale == 1 and dl[0].bench_scale == 8
    print("#15 OK: noise_limited (CI excludes 0, |Δ|<floor) vs improved at a lower floor")

    # --- #16: evaluate() auto-tightens noise-limited -> accepted; guards -----
    floors_by_scale = {1: hi, 8: lo}
    sig_by_scale = {1: (dh, ah), 8: (dl, al)}
    orig_sig, orig_cal = _evalmod._significance, _evalmod.calibrate_floors
    try:
        _evalmod._significance = lambda t, b, w, ab, scale, om, o, fl: sig_by_scale[scale]
        _evalmod.calibrate_floors = lambda t, b, runs, o, scale=1: floors_by_scale[scale]
        from aro.types import Candidate as _C, Patch as _P
        cand = _C(id="c", hypothesis="x", patch=_P([_E(FAST, "a", "b")]))
        out = _evalmod.evaluate(MockTarget(), "base", _P([]), cand, 4, hi, objs,
                                aa_runs=2, bench_scales=(1, 8))
        assert out.verdict == Verdict.ACCEPTED, out.verdict          # tightened past the floor
        assert out.deltas[0].bench_scale == 8, out.deltas[0].bench_scale
        # sign-disagreement guard: scale-8 Δ flips sign -> refuse to accept, stay noise-limited
        dl2 = [type(d)(**{**d.__dict__, "delta_pct": +3.0, "improved": False,
                          "noise_limited": False}) for d in dl]
        _evalmod._significance = lambda t, b, w, ab, scale, om, o, fl: (
            (dh, ah) if scale == 1 else (dl2, {"improved": False, "regressed": False, "noise_limited": False}))
        out2 = _evalmod.evaluate(MockTarget(), "base", _P([]), cand, 4, hi, objs,
                                 aa_runs=2, bench_scales=(1, 8))
        assert out2.verdict == Verdict.NOISE_LIMITED, out2.verdict   # guard refused the flipped "win"
    finally:
        _evalmod._significance, _evalmod.calibrate_floors = orig_sig, orig_cal
    print("#16 OK: auto-tighten noise-limited->accepted; sign-guard keeps it honest")
    print("SELFTEST PASSED")


if __name__ == "__main__":
    run()
