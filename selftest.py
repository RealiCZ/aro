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
    # data contract: candidate_proposed carries `lens` (explore-mode technique axis) and
    # `tokens` (the perf-vs-cumulative-token chart's X-axis), read from the log not re-derived.
    for key in ("lens", "tokens"):
        assert all(key in e for e in ev if e["event"] == "candidate_proposed"), \
            f"candidate_proposed must emit a {key} field"
    print(f"#6 OK: {len(ev)} events, all gates traced {sorted(gates)}; candidate_proposed carries lens+tokens")

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

    # --- #17: aro sweep — owner classify + frontier bucketing (deterministic) -
    from aro import sweep as _sw
    assert _sw.classify_owner("x_keccak_p1600_armv8_sha3", "mega_evm")[0] == "crypto"
    assert _sw.classify_owner("x_hashbrown_rustc_entry", "mega_evm")[0] == "runtime"
    assert _sw.classify_owner("x_8mega_evm3evm_compute_gas_ext", "mega_evm")[0] == "ours"
    ranked = [
        ("p1600_armv8_sha3", 20.0, "nt_keccak_p1600_armv8_sha3"),       # crypto
        ("rustc_entry", 8.0, "nt_hashbrown_rustc_entry"),               # runtime
        ("compute_gas_ext", 5.8, "8mega_evm3evm12instructions_compute_gas_ext"),  # ours untried
        ("check_limit", 3.7, "8mega_evm5limit_check_limit"),            # ours tried
        ("sstore", 2.5, "8mega_evm3evm12instructions_additional_limit_ext_sstore"),  # ours gated
        ("tiny", 0.4, "8mega_evm_tiny"),                                # below min_pct
    ]
    lessons_idx = [
        ("check_limit fan-out reduction", "within-noise", False),
        ("sstore storage_gas layer — reviewer architecture objection", "regressed", True),
    ]
    bk = _sw.bucket_functions(ranked, "mega_evm", lessons_idx, min_pct=1.5)
    assert [r["name"] for r in bk["untried"]] == ["compute_gas_ext"], bk["untried"]
    assert [r["name"] for r in bk["tried"]] == ["check_limit"], bk["tried"]
    assert [r["name"] for r in bk["gated"]] == ["sstore"], bk["gated"]
    assert {r["name"] for r in bk["not_ours"]} == {"p1600_armv8_sha3", "rustc_entry"}
    assert all(r["name"] != "tiny" for key in ("untried", "tried", "gated", "not_ours")
               for r in bk[key])   # below threshold dropped
    rep = _sw.render_map(bk, "demo", "hotloop", 1.5)
    assert "Actionable frontier" in rep and "`compute_gas_ext`" in rep
    assert "needs a human call" in rep and "Not our lever" in rep
    print("#17 OK: sweep classifies owner + buckets the frontier (untried/tried/gated/not-ours)")

    # --- #18: aro sweep --attempt — pure pieces (locate-grep, summarize, render) -
    from aro.types import EvalOutcome as _EO, MetricDelta as _MD, Candidate as _Cd, Patch as _Pt
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.rs").write_text("pub fn inspect_storage(&self) -> u64 { 0 }\n")
        # word-boundary: `inspect_storage_helper` must NOT match (underscore is a word
        # char, so `\binspect_storage\b` stops at it); a comment mention must NOT match.
        (src / "sub" / "b.rs").write_text("fn other() {}\nfn inspect_storage_helper() {}\n")
        (src / "c.rs").write_text("// only mentions inspect_storage in a comment\n")
        hits = _sw._grep_fn_files(src, "inspect_storage")
        assert [h.name for h in hits] == ["a.rs"], [h.name for h in hits]

    def _mk(verdict, dpct, improved, minimize=True):
        d = _MD(metric="ns", baseline=1.0, candidate=1.0, delta_pct=dpct,
                ci_low_pct=dpct, ci_high_pct=dpct, floor_pct=1.0,
                improved=improved, regressed=False)
        return (_Cd(id="x", hypothesis="h", patch=_Pt([])),
                _EO(candidate_id="x", verdict=verdict, deltas=[d], notes=["n"]))

    class _Rep:           # minimal stand-in for engine.Report (only .outcomes is read)
        outcomes: list = []
    rep = _Rep(); rep.outcomes = [_mk(Verdict.WITHIN_NOISE, +0.1, False),
                                  _mk(Verdict.ACCEPTED, -11.6, True)]
    v, dl = _sw._summarize_report(rep, {"ns": True})
    assert v == "accepted" and abs(dl - (-11.6)) < 1e-9, (v, dl)   # accept outranks; its Δ
    repm = _Rep(); repm.outcomes = [_mk(Verdict.ACCEPTED, +9.0, True, minimize=False)]
    _vm, dm = _sw._summarize_report(repm, {"ns": False})           # maximize: +Δ is the win
    assert abs(dm - 9.0) < 1e-9, dm

    rows = [{"name": "inspect_storage", "pct": 11.6, "verdict": "accepted",
             "delta": -11.6, "files": ["crates/x/src/a.rs"], "accepted": True},
            {"name": "convert", "pct": 2.0, "verdict": "noise-limited",
             "delta": -0.3, "files": ["crates/x/src/c.rs"], "accepted": False},
            {"name": "ghost", "pct": 1.7, "verdict": "unlocated",
             "delta": None, "files": [], "accepted": False}]
    am = _sw.render_attempt_map(rows, "demo", [object(), object()], max_attempts=6)
    assert "1 accepted" in am and "Comprehension debt" in am, am
    assert "`inspect_storage`" in am and "-11.60%" in am and "_(unlocated)_" in am

    # divergence escalation: when untried dries, refill from untried+tried+gated
    # heaviest-first, each fn until the per-fn try cap — this is what makes it NOT stop.
    bk2 = {"untried": [{"name": "a", "pct": 5.0}],
           "tried": [{"name": "b", "pct": 9.0}],
           "gated": [{"name": "c", "pct": 3.0}], "not_ours": []}
    q = _sw._refill_queue(bk2, tries={"a": 1}, cap=1)        # a exhausted (1>=cap)
    assert [r["name"] for r in q] == ["b", "c"], q           # heaviest-first, a dropped
    q2 = _sw._refill_queue(bk2, tries={}, cap=2)             # nothing tried yet
    assert [r["name"] for r in q2] == ["b", "a", "c"], q2    # 9 > 5 > 3
    assert _sw._refill_queue(bk2, tries={"a": 2, "b": 2, "c": 2}, cap=2) == []  # all capped
    # per-attempt seeded memory (the id-collision fix): cumulative resumes under unique ids
    from aro.types import Edit as _Ed
    with tempfile.TemporaryDirectory() as td2:
        m = _sw._seed_memory(Path(td2) / "a1", [_Ed("f.rs", "a", "b"), _Ed("g.rs", "c", "d")])
        ed = m.accepted_edits()
        assert [e.path for e in ed] == ["f.rs", "g.rs"], ed   # both, in order, no collision
    print("#18 OK: --attempt locate-grep + summarize + debt render + refill + seeded-compound")

    # --- #19: trajectory compounding (events.jsonl -> staircase) + chart render --
    import xml.etree.ElementTree as _ET
    from aro import trajectory as _tj, chart as _ch

    def _run_dir(td, name, dpct):
        d = Path(td) / name
        d.mkdir(parents=True)
        rid = "RUN1"
        evs = [
            {"run_id": rid, "event": "run_started"},
            {"run_id": rid, "event": "candidate_proposed", "id": "c1",
             "hypothesis": "host::inspect_storage REX4"},
            {"run_id": rid, "event": "candidate_verdict", "id": "c1",
             "verdict": "accepted",
             "deltas": [{"metric": "ns", "delta_pct": dpct, "improved": True}]},
            {"run_id": rid, "event": "run_finished"},
        ]
        (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in evs) + "\n")
        return str(d)

    with tempfile.TemporaryDirectory() as td:
        r2 = _run_dir(td, "r2", -11.62)
        r3 = _run_dir(td, "r3", -4.96)
        t = _tj.stitch([r2, r3], "convergent", converged=True)
        assert [s.accepted for s in t.steps] == [True, True], t.steps
        # COMPOUNDING, not summing: (1-.1162)(1-.0496)-1 = -16.0%, not -16.58%
        assert abs(t.final_pct - (-16.004)) < 0.05, t.final_pct
        assert t.steps[0].speedup_pct > 0 and t.steps[1].speedup_pct > t.steps[0].speedup_pct
        a = _ch.ascii_chart([t])
        assert "16.0% faster" in a and "converged" in a, a
        s = _ch.svg([t])
        _ET.fromstring(s)                       # well-formed XML
        assert "speedup" in s and "converged (plateau)" in s
        # a relaxed-oracle step renders dashed (a weaker-claim win must look different)
        t.steps[1].regime = "relaxed"
        assert "stroke-dasharray" in _ch.svg([t])
    print("#19 OK: trajectory compounds (not sums); chart renders valid SVG + regime dashing")

    # --- #20: explorer — headroom / floor / continue-stop decision + report ------
    bk3 = {"untried": [{"name": "a", "pct": 5.0}, {"name": "b", "pct": 3.0}],
           "tried": [{"name": "c", "pct": 2.0}],
           "not_ours": [{"name": "keccak", "pct": 52.0}, {"name": "revm", "pct": 10.0}]}
    assert _sw._addressable(bk3, set()) == 10.0                  # 5+3+2 open
    assert _sw._addressable(bk3, {"a"}) == 5.0                   # a attempted → drops out
    assert _sw._floor_pct(bk3) == 62.0                          # 52+10 not-ours
    assert _sw._explore_decision(10.0, 0)[0] == "CONTINUE"
    assert _sw._explore_decision(1.5, 0)[0] == "STOP"            # headroom drained
    assert _sw._explore_decision(8.0, 3)[0] == "STOP"            # diminishing returns
    elog = [{"i": 1, "fn": "check_limit", "verdict": "within-noise", "delta": -0.3,
             "accepted": False, "regime": "byte-identical", "realized_cum": 0.0, "headroom": 5.0},
            {"i": 2, "fn": "inspect_storage", "verdict": "accepted", "delta": -4.96,
             "accepted": True, "regime": "byte-identical", "realized_cum": -4.96, "headroom": 2.0}]
    rep = _sw.render_explore_report(elog, "demo", "evm_r3", 52.0, "STOP", "drained")
    assert "进化了" in rep and "能进化的" in rep and "判定" in rep and "STOP" in rep
    assert "5.0% faster" in rep                                  # realized = -(-4.96)
    es = _ch.explore_svg(elog, 52.0, "STOP", "drained", "demo")
    _ET.fromstring(es)
    assert "判定 STOP" in es and "addressable headroom" in es
    # headroom drops colored by cause: a failed-attempt drop = ✗排除, a win drop = ✓捕获
    drop_elog = [{"i": 1, "fn": "a", "verdict": "within-noise", "delta": -0.1, "accepted": False,
                  "regime": "byte-identical", "realized_cum": 0.0, "headroom": 8.0},
                 {"i": 2, "fn": "b", "verdict": "within-noise", "delta": 0.1, "accepted": False,
                  "regime": "byte-identical", "realized_cum": 0.0, "headroom": 5.0},   # fail drop
                 {"i": 3, "fn": "c", "verdict": "accepted", "delta": -3.0, "accepted": True,
                  "regime": "byte-identical", "realized_cum": -3.0, "headroom": 2.0}]   # win drop
    es2 = _ch.explore_svg(drop_elog, 50.0, "CONTINUE", "x", "demo")
    _ET.fromstring(es2)
    assert "✗ 排除" in es2 and "✓ 捕获" in es2, "headroom drop cause not colored"

    # demangle-leaf parse (the fix that un-hid the real levers): fn name vs generic args
    assert _sw._demangle_leaf("<revm_context::journal::Journal<revm_database::in_memory_db"
        "::CacheDB<core::convert::Infallible>> as mega_evm::evm::host::JournalInspectTr>"
        "::inspect_storage") == "inspect_storage"
    assert _sw._demangle_leaf("mega_evm::evm::host::inspect_account::<revm_database"
        "::in_memory_db::CacheDB<core::convert::Infallible>>") == "inspect_account"
    assert _sw._demangle_leaf("mega_evm::evm::instructions::compute_gas_ext::push1::<"
        "revm_interpreter::interpreter::EthInterpreter, mega_evm::evm::context"
        "::MegaContext<core::convert::Infallible>>") == "push1"
    assert _sw._demangle_leaf("<mega_evm::limit::limit::AdditionalLimit>::check_limit") == "check_limit"
    assert _sw._demangle_leaf("foldhash::hash_bytes_long") == "hash_bytes_long"
    # honest headroom split: locatable counts toward the decision, un-locatable does not
    bk4 = {"untried": [{"name": "a", "pct": 5.0}, {"name": "ghost", "pct": 30.0}],
           "tried": [{"name": "b", "pct": 2.0}]}
    addr, unreach = _sw._split_headroom(bk4, set(), lambda n: n != "ghost")
    assert addr == 7.0 and unreach == 30.0, (addr, unreach)   # ghost un-locatable → excluded
    assert _sw._split_headroom(bk4, {"a"}, lambda n: n != "ghost")[0] == 2.0  # a attempted
    print("#20 OK: explorer headroom/floor + decision + demangle-leaf + honest reachable split")

    # --- #21: infinite-flow — exhaustive decision + lens ladder + dedup + prescreen -
    # 4.4 exhaustive: the cost-saving cross-fn dry-stop is dropped, but drained headroom
    # still stops (and the legacy dry-stop is intact when exhaustive is off).
    assert _sw._explore_decision(8.0, 5, exhaustive=True)[0] == "CONTINUE"   # dry-stop gone
    assert _sw._explore_decision(1.0, 5, exhaustive=True)[0] == "STOP"       # headroom still stops
    assert _sw._explore_decision(8.0, 3, exhaustive=False)[0] == "STOP"      # legacy dry-stop intact
    # 4.1 lens ladder: candidate k in round r climbs micro → layout → algorithm
    from aro.generator import _lens_for, _lens_text
    assert _lens_for(0, 0)[0] == "micro-elimination"
    assert _lens_for(0, 1)[0] == "data-layout / allocation"
    assert _lens_for(0, 2)[0] == "algorithm" and _lens_for(9, 0)[0] == "algorithm"  # clamps
    assert "lens for THIS attempt" in _lens_text(_lens_for(0, 0))
    # 4.3b dedup: identical patches collapse to one (judge each change once)
    from aro.eval import dedup_candidates as _dd
    from aro.types import Candidate as _C2, Patch as _P2
    _ed = Edit(FAST, "x", "y")
    _du = _dd([_C2("d1", "", _P2([_ed])), _C2("d2", "", _P2([Edit(FAST, "x", "y")])),
               _C2("d3", "", _P2([Edit("o.rs", "p", "q")]))])
    assert [c.id for c in _du] == ["d1", "d3"], _du     # d2 == d1 textually → dropped

    # 4.2 + 4.3b end-to-end: a fanned-out round (3 candidates: a slow one, a fast one,
    # and an exact-duplicate fast one) runs through engine prescreen → dedup → priority
    # order → serial judge. The dup is judged once; the fast one (better smoke Δ) is
    # ordered FIRST and accepted; junk never reorders ahead of a real win.
    class _FanoutGen:
        name = "fanout-mock"

        def __init__(self, by_round):
            self.by_round = by_round

        def propose(self, ctx, n):
            return [_C2(cid, hyp, _P2(list(eds)))
                    for cid, hyp, eds in self.by_round.get(ctx.round, [])]

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tg = MockTarget()
        gen = _FanoutGen({0: [
            ("s1", "slow change", [Edit("src/other.rs", "p", "q")]),   # not FAST → ~0 smoke
            ("f1", "fast change", [Edit(FAST, "x", "y")]),             # FAST → +5% smoke
            ("f2", "fast dup",   [Edit(FAST, "x", "y")]),             # identical to f1 → deduped
        ]})
        evs = EventLog(d / "events.jsonl", also_console=False)
        rep = run_backtest(tg, gen, Memory(d), rounds=2, candidates_per_round=3,
                           aa_runs=2, ab_pairs=4, baseline_ref="HEAD", events=evs,
                           stop_dry_rounds=1, prescreen=True)
        ids = [c.id for c, _o in rep.outcomes]
        assert "f2" not in ids, ids                       # exact duplicate judged 0 times
        accepted = [c.id for c, o in rep.outcomes if o.verdict == Verdict.ACCEPTED]
        assert "f1" in accepted, (ids, accepted)          # the fast change wins
        elog = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines() if l.strip()]
        ordered = next(e for e in elog if e.get("event") == "prescreen_ordered")
        assert ordered["order"][0] == "f1", ordered        # better smoke Δ judged first
        assert "f2" not in ordered["order"], ordered       # dup not even in the queue
        pres = {e["id"] for e in elog if e.get("event") == "prescreen"}
        assert pres == {"f1", "s1"}, pres                  # one screen per deduped survivor
    print("#21 OK: infinite-flow exhaustive-stop + lens ladder + dedup + prescreen-priority")

    # --- #22: critic gate (the SECOND judge) — pure gate logic with a mock reviewer ---
    from aro import critic as _cr
    _mock = lambda ans: (lambda prompt: ans)
    # pass → passes the gate
    c = _cr.critique("code", "diff", "ctx", runner=_mock('{"verdict":"pass","reasons":[]}'))
    assert c.passed and c.verdict == "pass", c
    # reject (known-bad pattern) → does NOT pass; structured reasons preserved for the audit
    c = _cr.critique("code", "inline+delete layer", "ctx", runner=_mock(
        '{"verdict":"reject","reasons":[{"rubric":"layer-dissolve",'
        '"finding":"deletes storage_gas_ext","severity":"high","example":"PR#313"}]}'))
    assert not c.passed and c.verdict == "reject"
    assert c.reasons[0].rubric == "layer-dissolve" and c.reasons[0].example == "PR#313"
    assert c.as_event()["reasons"][0]["severity"] == "high"
    # pass-risk → PASSES the gate (the risk is flagged + recorded, not blocked)
    c = _cr.critique("code", "x", "", runner=_mock(
        '{"verdict":"pass-risk","reasons":[{"rubric":"cross-crate","finding":"edits a fork"}]}'))
    assert c.passed and c.verdict == "pass-risk"
    # token capture: a runner returning (text, output_tokens) records the review's spend
    ct = _cr.critique("code", "x", "", runner=lambda p: ('{"verdict":"pass","reasons":[]}', 137))
    assert ct.verdict == "pass" and ct.tokens == 137, ct
    # default-REJECT on un-gradeable output (an un-parseable review is NOT a pass)
    assert _cr.critique("code", "x", "", runner=_mock("looks fine to me")).verdict == "reject"
    assert _cr.critique("code", "x", "", runner=_mock('{"verdict":"maybe"}')).verdict == "reject"
    # default-REJECT when the reviewer can't run (maker-checker: never silently pass un-reviewed)
    def _boom(p):
        raise RuntimeError("claude down")
    cb = _cr.critique("code", "x", "", runner=_boom)
    assert cb.verdict == "reject" and cb.reasons[0].rubric == "critic-unavailable"
    # N-vote hook: a reject majority/tie rejects; otherwise the worst survivor (pass-risk > pass)
    s1 = iter(['{"verdict":"pass","reasons":[]}', '{"verdict":"reject","reasons":[]}',
               '{"verdict":"pass","reasons":[]}'])
    assert _cr.critique("code", "x", "", n=3, runner=lambda p: next(s1)).verdict == "pass"
    s2 = iter(['{"verdict":"reject","reasons":[]}', '{"verdict":"reject","reasons":[]}',
               '{"verdict":"pass","reasons":[]}'])
    assert _cr.critique("code", "x", "", n=3, runner=lambda p: next(s2)).verdict == "reject"
    s3 = iter(['{"verdict":"pass","reasons":[]}', '{"verdict":"pass-risk","reasons":[]}'])
    assert _cr.critique("code", "x", "", n=2, runner=lambda p: next(s3)).verdict == "pass-risk"
    # all three rubric kinds load their prompt template
    for k in ("plan", "bench", "code"):
        assert _cr.critique(k, "art", "ctx",
                            runner=_mock('{"verdict":"pass","reasons":[]}')).verdict == "pass"
    print("#22 OK: critic gate — pass/reject/pass-risk + default-reject + N-vote majority + 3 rubrics")

    # --- #23: critic gate WIRED into evaluate — runs AFTER apply+build, SKIPS the bench --
    from aro.types import Candidate as _C3, Patch as _P3, Edit as _E3
    from aro import critic as _cr2

    class _OneGen:
        name = "one"

        def propose(self, ctx, n):
            return [_C3(id="cg", hypothesis="fast edit", patch=_P3([_E3(FAST, "x", "y")]))]

    # a REJECT critic → candidate is recorded REJECTED and skips the scarce serial bench
    reject_critic = lambda kind, art, ctx: _cr2.Critique(
        "reject", [_cr2.Reason("reward-hack", "gamed the bench", "high")])
    with tempfile.TemporaryDirectory() as d:
        d = Path(d); ev = EventLog(d / "events.jsonl", also_console=False)
        rep = run_backtest(MockTarget(), _OneGen(), Memory(d), rounds=1,
                           candidates_per_round=1, aa_runs=2, ab_pairs=4,
                           baseline_ref="HEAD", events=ev, critic=reject_critic)
        assert [(c.id, o.verdict) for c, o in rep.outcomes] == [("cg", Verdict.REJECTED)]
        assert not rep.pareto                                   # nothing accepted
        evs = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines() if l.strip()]
        cev = next(e for e in evs if e.get("event") == "critic")
        assert cev["verdict"] == "reject" and cev["reasons"][0]["rubric"] == "reward-hack"
        # NEW ordering (以防浪费): apply+build run FIRST (cheap) so the critic is never spent
        # on a non-applying patch — but the SCARCE serial bench is still skipped on a reject.
        cg_gates = [e.get("gate") for e in evs if e.get("event") == "gate" and e.get("candidate") == "cg"]
        assert "build" in cg_gates, cg_gates                    # apply+build DID run, then the critic gated
        assert "significance" not in cg_gates and "test" not in cg_gates, cg_gates  # bench saved
    # a PASS critic → the same FAST candidate proceeds to the bench and is accepted
    pass_critic = lambda kind, art, ctx: _cr2.Critique("pass", [])
    with tempfile.TemporaryDirectory() as d:
        d = Path(d); ev = EventLog(d / "events.jsonl", also_console=False)
        rep = run_backtest(MockTarget(), _OneGen(), Memory(d), rounds=1,
                           candidates_per_round=1, aa_runs=2, ab_pairs=4,
                           baseline_ref="HEAD", events=ev, critic=pass_critic)
        assert any(o.verdict == Verdict.ACCEPTED for _, o in rep.outcomes), rep.outcomes
    print("#23 OK: critic after apply+build (no waste), still skips the scarce bench; pass proceeds")

    # --- #24: drift fix — a candidate's whole-file SEARCH is anchored to the base edit's
    #          EXACT replace, NOT a git-normalized blob, so apply(base)+apply(candidate)
    #          chains byte-exactly (the bug that failed a 2nd-attempt edit to an accepted file) --
    from aro.generator import AgenticGenerator as _AG
    from aro import generator as _genmod

    ORIG = "fn host() { 1 }\n"
    BASE = "fn host() { 2 }\n"      # a prior accept's EXACT on-disk result (judge applies this)
    FINAL = "fn host() { 3 }\n"     # this attempt's agent edit, made on top of BASE
    base_edits = [Edit("h.rs", ORIG, BASE)]

    class _R:                       # minimal CompletedProcess stand-in
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout; self.returncode = returncode

    with tempfile.TemporaryDirectory() as d:
        scratch = Path(d); (scratch / "h.rs").write_text(FINAL)

        def _fake_run(argv, *a, **k):
            if "status" in argv:
                return _R(" M h.rs\n")
            if "show" in argv:
                return _R(BASE + "\n")    # git blob round-trip ADDS a newline — the drift
            return _R()

        orig_run = _genmod.subprocess.run
        try:
            _genmod.subprocess.run = _fake_run
            edits = _AG(object())._diff_to_edits(scratch, base_edits)
        finally:
            _genmod.subprocess.run = orig_run

    assert len(edits) == 1, edits
    assert edits[0].search == BASE, repr(edits[0].search)     # anchored to base.replace, NOT BASE+"\n"
    assert edits[0].replace == FINAL, repr(edits[0].replace)

    def _apply(content, e):         # mirrors SpecTarget.apply's unique-search rule
        assert content.count(e.search) == 1, "search not found / not unique"
        i = content.find(e.search)
        return content[:i] + e.replace + content[i + len(e.search):]

    work = _apply(ORIG, base_edits[0])         # judge applies the base → BASE on disk
    assert work == BASE
    work = _apply(work, edits[0])              # judge applies the candidate → FINAL (no drift)
    assert work == FINAL
    drifted = False                            # the OLD git-blob anchor would NOT have applied
    try:
        _apply(BASE, Edit("h.rs", BASE + "\n", FINAL))
    except AssertionError:
        drifted = True
    assert drifted, "expected the git-blob anchor to break apply (the drift this fix removes)"
    print("#24 OK: drift fixed — SEARCH anchored to the base edit's exact replace, chains byte-exact")

    # --- #25: perf-vs-token chart — running-best speedup over cumulative LLM tokens --------
    from aro import chart as _ch
    pev = [
        {"event": "attempt_started", "fn": "sstore", "regime": "byte-identical"},
        {"event": "candidate_proposed", "id": "a0", "lens": "micro-elimination", "tokens": 4000},
        {"event": "critic", "id": "a0", "tokens": 1000, "verdict": "pass"},
        {"event": "candidate_verdict", "id": "a0", "verdict": "within-noise",
         "deltas": [{"delta_pct": -0.3}]},
        {"event": "candidate_proposed", "id": "a1", "lens": "algorithm", "tokens": 6000},
        {"event": "candidate_verdict", "id": "a1", "verdict": "accepted",
         "deltas": [{"delta_pct": -10.0}]},
        {"event": "candidate_proposed", "id": "a2", "tokens": 5000},
        {"event": "candidate_verdict", "id": "a2", "verdict": "build-failed", "deltas": []},
        {"event": "profile_floor", "frames": [{"pct": 40.0}, {"pct": 10.0}]},
    ]
    pd = _ch._perf_data(pev)
    assert pd["have_tokens"] and pd["cum_tok"] == 16000, pd["cum_tok"]   # 4000+1000+6000+5000
    assert abs(pd["realized"] - 10.0) < 1e-9, pd["realized"]             # one -10% accept compounds
    assert abs(pd["ceiling"] - 50.0) < 1e-9, pd["ceiling"]               # floor 50% -> 50% Amdahl bound
    assert pd["n"] == 3 and len(pd["steps"]) == 2, (pd["n"], len(pd["steps"]))  # 3 cands, 1 accept step
    svg = _ch.perf_token_svg(pev, "demo")
    assert svg.startswith("<svg") and "cumulative output tokens" in svg and "running best" in svg
    # no-token run degrades to the candidate-# axis instead of breaking
    npd = _ch._perf_data([{"event": "candidate_proposed", "id": "x"},
                          {"event": "candidate_verdict", "id": "x", "verdict": "within-noise",
                           "deltas": [{"delta_pct": 0.1}]}])
    assert not npd["have_tokens"] and npd["n"] == 1
    assert "candidate #" in _ch.perf_token_svg([], "empty")  # empty run still renders
    print("#25 OK: perf/token chart — running-best vs cumulative tokens, off-spec marks, Amdahl ceiling")
    print("SELFTEST PASSED")


if __name__ == "__main__":
    run()
