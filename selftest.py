"""Cargo-free self-test: 21 isolated case groups covering the deterministic core
(compounding, event log, judge math, prescreen, probe/workload factories,
permtree, CLI parsing seams) with mock targets. No cargo, no model, no network.
A failing group never masks the rest; the runner reports every failure."""
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

# Shared across case groups: type aliases + the split-module namespace shim
# (sweep's pure helpers now live in symbols/frontier/report_md/attempt).
from aro.types import Patch as _P, Edit as _E   # noqa: E402
import xml.etree.ElementTree as _ET               # noqa: E402
import types as _types                           # noqa: E402
from aro import attempt as _at, frontier as _fr, report_md as _rm, symbols as _sy  # noqa: E402
_sw = _types.SimpleNamespace(
    classify_owner=_sy.classify_owner, _demangle_leaf=_sy._demangle_leaf,
    bucket_functions=_fr.bucket_functions, _grep_fn_files=_fr._grep_fn_files,
    _refill_queue=_fr._refill_queue, _addressable=_fr._addressable,
    _floor_pct=_fr._floor_pct, _split_headroom=_fr._split_headroom,
    _explore_decision=_fr._explore_decision,
    render_map=_rm.render_map, render_explore_report=_rm.render_explore_report,
    render_attempt_map=_rm.render_attempt_map,
    _summarize_report=_at._summarize_report, _seed_memory=_at._seed_memory,
    _probe_rescue=_at._probe_rescue)


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


def case_01():
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


def case_02():
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


def case_03():
    # --- #8: regression gate parser (N_pre) ----------------------------------
    from aro.target import _count_passed
    assert _count_passed("test result: ok. 12 passed; 0 failed; 0 ignored") == 12
    assert _count_passed("test result: ok. 5 passed; 0 failed\n"
                         "test result: ok. 3 passed; 0 failed") == 8
    assert _count_passed("compiling... no tests ran") is None
    print("#8 OK: _count_passed sums cargo test totals (regression N_pre)")


def case_04():
    # --- #9: thin Ralph driver's block-format parser -------------------------
    from aro.generator import parse_response
    hyp, edits = parse_response(
        "noise\n@@HYPOTHESIS@@ hoist d*x*y\n@@FILE@@ src/a.rs\n"
        "@@SEARCH@@\nlet c = a*b;\n@@REPLACE@@\nlet c = ab;\n@@END@@\ntrailing")
    assert hyp == "hoist d*x*y" and len(edits) == 1 and edits[0].path == "src/a.rs"
    assert edits[0].search == "let c = a*b;" and edits[0].replace == "let c = ab;"
    assert parse_response("no blocks here") is None
    print("#9 OK: Ralph block-format parser (parse_response)")


def case_05():
    # --- #10: region guard enforced  +  #11: direction-aware judge -----------
    from aro.guard import screen as _screen
    from aro.eval import _judge_metric
    from aro.types import Edit as _E
    assert _screen(_P([_E("src/lib.rs", "a", "b")]), ["src/lib.rs"]) is None
    assert _screen(_P([_E("src/other.rs", "a", "b")]),
                   ["src/lib.rs"]) is not None                             # outside region
    assert _screen(_P([_E("pkg/sub/x.rs", "a", "b")]), ["pkg"]) is None    # under dir region
    assert _judge_metric(-2.0, -3.0, -1.0, 0.5, True) == (True, False)     # minimize win
    assert _judge_metric(+2.0, 1.0, 3.0, 0.5, False) == (True, False)      # maximize win
    assert _judge_metric(+2.0, 1.0, 3.0, 0.5, True) == (False, True)       # minimize regress
    assert _judge_metric(-0.2, -0.6, 0.2, 0.5, True) == (False, False)     # within noise
    print("#10/#11 OK: region guard enforced + direction-aware judge (min/maximize)")


def case_06():
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


def case_07():
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
                        "sample_prefix": "B", "metric": "tps", "cargo_args": []}
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


def case_08():
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


def case_09():
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


def case_11():
    # --- #17: aro sweep — owner classify + frontier bucketing (deterministic) -
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


def case_12():
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



def case_14():
    from aro import chart as _ch
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
    assert "Realized" in rep and "Addressable headroom" in rep and "Decision" in rep and "STOP" in rep
    assert "5.0% faster" in rep                                  # realized = -(-4.96)
    es = _ch.explore_svg(elog, 52.0, "STOP", "drained", "demo")
    _ET.fromstring(es)
    assert "decision STOP" in es and "addressable headroom" in es
    # headroom drops colored by cause: failed attempt = ruled out, win = captured
    drop_elog = [{"i": 1, "fn": "a", "verdict": "within-noise", "delta": -0.1, "accepted": False,
                  "regime": "byte-identical", "realized_cum": 0.0, "headroom": 8.0},
                 {"i": 2, "fn": "b", "verdict": "within-noise", "delta": 0.1, "accepted": False,
                  "regime": "byte-identical", "realized_cum": 0.0, "headroom": 5.0},   # fail drop
                 {"i": 3, "fn": "c", "verdict": "accepted", "delta": -3.0, "accepted": True,
                  "regime": "byte-identical", "realized_cum": -3.0, "headroom": 2.0}]   # win drop
    es2 = _ch.explore_svg(drop_elog, 50.0, "CONTINUE", "x", "demo")
    _ET.fromstring(es2)
    assert "✗ ruled out" in es2 and "✓ captured" in es2, "headroom drop cause not colored"

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


def case_15():
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
        # P3.5 worktree-reuse must not LEAK: every prescreen/judge worktree torn down
        assert tg._wt == {}, f"leaked worktrees: {list(tg._wt)}"
    print("#21 OK: infinite-flow exhaustive-stop + lens ladder + dedup + prescreen-priority"
          " + no worktree leak")


def case_16():
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


def case_17():
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
        # NEW ordering (no wasted spend): apply+build run FIRST (cheap) so the critic is never spent
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


def case_18():
    # --- #24: drift fix — a candidate's whole-file SEARCH is anchored to the base edit's
    #          EXACT replace, NOT a git-normalized blob, so apply(base)+apply(candidate)
    #          chains byte-exactly (the bug that failed a 2nd-attempt edit to an accepted file) --
    from aro.generator import AgenticGenerator as _AG

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

        from aro import vcs as _vcs
        orig_run = _vcs.subprocess.run
        try:
            _vcs.subprocess.run = _fake_run       # git plumbing now routes through aro.vcs
            edits = _AG(object())._diff_to_edits(scratch, base_edits)
        finally:
            _vcs.subprocess.run = orig_run

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


def case_19():
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


def case_20():
    # --- #26: round-end folding — siblings judged on a FROZEN base; best folds, the
    #          conflicting sibling is superseded (NOT apply-failed mid-evaluation) ----------
    from aro.types import Candidate as _C6, Patch as _P6, Edit as _E6

    class _FileTarget:
        """In-memory target with REAL search/replace apply (so a same-file sibling conflict
        actually fails at fold time, like SpecTarget). bench: each known content has a speed."""
        name = "filetgt"

        def __init__(self):
            self._wt, self._tick = {}, 0

        def objectives(self):
            return []

        def make_worktree(self, tag):
            self._tick += 1
            p = f"ft-{tag}-{self._tick}"; self._wt[p] = {"f.rs": "ABC"}; return p

        def remove_worktree(self, w):
            self._wt.pop(w, None)

        def apply(self, patch, work):
            f = self._wt[work]
            for e in patch.edits:
                c = f.get(e.path, "")
                if c.count(e.search) != 1:
                    raise RuntimeError(f"search text not found in {e.path}")
                i = c.find(e.search); f[e.path] = c[:i] + e.replace + c[i + len(e.search):]

        def build(self, work):
            pass

        def test(self, work):
            pass

        def differential(self, work, baseline):
            return True

        def bench(self, work, scale=1):
            c = self._wt.get(work, {}).get("f.rs", "ABC")
            sp = {"ABC": 100.0, "XYZW": 85.0, "PQ": 92.0}.get(c, 100.0)  # both beat base
            m = Metrics(); m.put("metric/x", [sp, sp, sp]); return m

    class _TwoSib:
        name = "twosib"

        def propose(self, ctx, n):
            if ctx.round != 0:
                return []
            return [_C6(id="cA", hypothesis="big", patch=_P6([_E6("f.rs", "ABC", "XYZW")])),
                    _C6(id="cB", hypothesis="small", patch=_P6([_E6("f.rs", "ABC", "PQ")]))]

    with tempfile.TemporaryDirectory() as d:
        d = Path(d); ev = EventLog(d / "events.jsonl", also_console=False)
        rep = run_backtest(_FileTarget(), _TwoSib(), Memory(d), rounds=1,
                           candidates_per_round=2, aa_runs=2, ab_pairs=4,
                           baseline_ref="HEAD", events=ev)
        vd = {c.id: o.verdict for c, o in rep.outcomes}
        # BOTH win on the frozen base — the loser is NOT apply-failed (the old bug)
        assert vd == {"cA": Verdict.ACCEPTED, "cB": Verdict.ACCEPTED}, vd
        evs = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines() if l.strip()]
        adv = [e["by"] for e in evs if e["event"] == "baseline_advanced"]
        sup = [e["id"] for e in evs if e["event"] == "candidate_superseded"]
        assert adv == ["cA"], adv          # only the strongest (15% > 8%) folded
        assert sup == ["cB"], sup          # the conflicting sibling: superseded, not failed
        # cB never apply-failed during evaluation (no failed gate event for it)
        assert not any(e.get("event") == "gate" and e.get("candidate") == "cB"
                       and e.get("status") == "fail" for e in evs)
        # the meta-loop adopts exactly the folded win, never the superseded sibling
        assert [e.path for e in rep.folded_edits] == ["f.rs"], rep.folded_edits
        assert rep.folded_edits[0].replace == "XYZW", rep.folded_edits[0]
    print("#26 OK: round-end folding — siblings fair on a frozen base; best folds, loser superseded")


def case_21():
    # --- #27: manifest reconstruction (the hand-off artifact) ----------------
    # An OLD-format run (no `attempt` stamp) with the id collision that breaks naive
    # consumers: agent-r0-0 is BOTH a relaxed/pass-risk win (a1) and a byte-identical/
    # pass win (a2). The manifest must resolve each to its own attempt dir + patch, and
    # mark only the clean byte-identical one mergeable.
    from aro import manifest as manifestmod
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        def J(o): return json.dumps(o)
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sstore",
             "regime": "relaxed", "files": ["crates/x/src/a.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass-risk"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -19.2, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "cache"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        for a, repl in (("a1", "crates/x/src/a.rs"), ("a2", "crates/x/src/b.rs")):
            pd = d / a / "patches"; pd.mkdir(parents=True)
            (pd / "agent-r0-0.txt").write_text(
                f"--- edit 1 ---\npath: {repl}\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        m = manifestmod.build_manifest(d)
        assert m["baseline_ref"] == "abc123" and m["spec"] == "demo", m
        acc = m["accepted"]
        assert [a["attempt"] for a in acc] == ["a1", "a2"], acc        # collision resolved by attempt
        assert acc[0]["fn"] == "sstore" and acc[0]["files"] == ["crates/x/src/a.rs"]
        assert acc[1]["fn"] == "sload" and acc[1]["files"] == ["crates/x/src/b.rs"]
        assert acc[0]["delta_pct"] == -19.2 and acc[1]["delta_pct"] == -4.5
        assert acc[0]["mergeable"] is False                            # relaxed/pass-risk
        assert acc[1]["mergeable"] is True                             # byte-identical/pass
        assert acc[0]["patch_path"] == "a1/patches/agent-r0-0.txt"
        assert m["files_touched"] == ["crates/x/src/a.rs", "crates/x/src/b.rs"], m
    print("#27 OK: manifest resolves id-collision by attempt + flags only clean byte-identical mergeable")


def case_22():
    # --- #28: L4a probe rescue — author→qualify(frozen)→re-judge→parent gate, all hooked ----
    from aro import probe_factory as _pf
    from aro import spec as _specmod
    from aro.types import NoiseFloors as _NF
    from aro.types import (Candidate, EvalOutcome, MetricDelta, Patch, Report)

    pfspec = _specmod.from_dict({
        "name": "probetest", "target_repo": {"path": "."}, "metric": "ns",
        "hot_path": {"file": "src/lib.rs", "fn": "hotfn"},
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "k"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]}})
    parent_floors = _NF(); parent_floors.put("ns", 2.0)
    probe_rel = _pf.probe_rel_path("probetest", "hotfn")
    ppath = Path(_pf.REPO_ROOT) / probe_rel
    ppath.parent.mkdir(parents=True, exist_ok=True)

    def _author(spec_, fn_, files_):
        ppath.write_text("// canned micro-probe")
        return probe_rel

    def _bench(mspec, scale=1):
        m = Metrics(); m.put("ns", [100.0, 100.02, 99.98, 100.01, 100.0])
        return m

    def _mkreport(accept: bool):
        e2 = Edit("src/lib.rs", "slow", "fast")
        c2 = Candidate(id="micro-c", hypothesis="micro win", patch=Patch([e2]))
        o2 = EvalOutcome("micro-c", Verdict.ACCEPTED if accept else Verdict.WITHIN_NOISE,
                         [MetricDelta("ns", 100, 96, -4.0, -4.4, -3.6, 0.5, True, False)],
                         [])
        rep = Report(target="probetest", baseline_ref="HEAD", rounds=1,
                     floors=_NF(), outcomes=[(c2, o2)])
        rep.folded_edits = [e2] if accept else []
        return rep

    class _Ev:
        def __init__(self): self.events = []; self.context = {}
        def emit(self, ev, **f): self.events.append((ev, f))

    with tempfile.TemporaryDirectory() as d:
        # (a) author fails → no row, no fold, traceable event
        ev = _Ev()
        ran2, row, ne = _sw._probe_rescue(
            pfspec, pfspec, "hotfn", ["src/lib.rs"], 5.0, parent_floors, {"ns": True},
            [], Path(d), 3, ev, fanout=1, gen_concurrency=1, rounds_per_fn=1,
            prescreen=False, critic=None, per_fn_dry=1,
            hooks={"parent_covers": lambda *a, **k: True,
                   "author": lambda *a: (_ for _ in ()).throw(RuntimeError("no agent"))})
        assert (ran2, row, ne) == (3, None, []) and ev.events[-1][0] == "probe_author_failed"

        # (b) qualified + accepted + parent-ok → folds, regime micro-proven, frozen sha
        ev = _Ev()
        ran2, row, ne = _sw._probe_rescue(
            pfspec, pfspec, "hotfn", ["src/lib.rs"], 5.0, parent_floors, {"ns": True},
            [], Path(d), 3, ev, fanout=1, gen_concurrency=1, rounds_per_fn=1,
            prescreen=False, critic=None, per_fn_dry=1,
            hooks={"parent_covers": lambda *a, **k: True,
                   "author": _author, "bench": _bench,
                   "profile_shares": lambda s: {"hotfn": 85.0},
                   "rejudge": lambda mspec, r: _mkreport(True),
                   "parent_check": lambda *a: True})
        assert ran2 == 4 and row["regime"] == "micro-proven" and row["accepted"], row
        assert ne and ne[0].path == "src/lib.rs"
        names = [e for e, _ in ev.events]
        reg = dict(ev.events)["probe_registered"]
        assert reg["ok"] and reg["sha256"] and reg["relevance_pct"] == 85.0, reg
        assert names.index("probe_registered") < names.index("attempt_started"), \
            "probe must FREEZE before any candidate generation for the node"

        # (c) parent regression → win is NOT folded, verdict says why
        ev = _Ev()
        _, row, ne = _sw._probe_rescue(
            pfspec, pfspec, "hotfn", ["src/lib.rs"], 5.0, parent_floors, {"ns": True},
            [], Path(d), 3, ev, fanout=1, gen_concurrency=1, rounds_per_fn=1,
            prescreen=False, critic=None, per_fn_dry=1,
            hooks={"parent_covers": lambda *a, **k: True,
                   "author": _author, "bench": _bench,
                   "profile_shares": lambda s: {"hotfn": 85.0},
                   "rejudge": lambda mspec, r: _mkreport(True),
                   "parent_check": lambda *a: False})
        assert row["verdict"] == "parent-regressed" and not ne and not row["accepted"], row

        # (d) unqualified probe (low relevance) → no re-judge at all
        ev = _Ev()
        _, row, ne = _sw._probe_rescue(
            pfspec, pfspec, "hotfn", ["src/lib.rs"], 5.0, parent_floors, {"ns": True},
            [], Path(d), 3, ev, fanout=1, gen_concurrency=1, rounds_per_fn=1,
            prescreen=False, critic=None, per_fn_dry=1,
            hooks={"parent_covers": lambda *a, **k: True,
                   "author": _author, "bench": _bench,
                   "profile_shares": lambda s: {"hotfn": 20.0},
                   "rejudge": lambda mspec, r: (_ for _ in ()).throw(AssertionError("must not re-judge")),
                   "parent_check": lambda *a: True})
        assert row is None and not ne
        reg = dict(ev.events)["probe_registered"]
        assert not reg["ok"] and any("Q3" in r for r in reg["reasons"]), reg
        # (e) parent differential does NOT constrain the fn → weak-oracle node, no rescue
        ev = _Ev()
        _, row, ne = _sw._probe_rescue(
            pfspec, pfspec, "hotfn", ["src/lib.rs"], 5.0, parent_floors, {"ns": True},
            [], Path(d), 3, ev, fanout=1, gen_concurrency=1, rounds_per_fn=1,
            prescreen=False, critic=None, per_fn_dry=1,
            hooks={"parent_covers": lambda *a, **k: False,
                   "author": lambda *a: (_ for _ in ()).throw(AssertionError("must not author"))})
        assert row is None and not ne

        # mutator sanity: seeded mutation differs and stays inside the fn
        from aro.probe_factory import _mutate_fn_body
        rs = "fn hotfn(x: u64) -> u64 { x ^ 3 }\nfn other() -> u64 { 7 }\n"
        muts = list(_mutate_fn_body(rs, "hotfn"))
        assert muts and all("fn other() -> u64 { 7 }" in m for m in muts), muts
        # operators inside string literals are NOT mutation sites
        rs2 = 'fn hotfn(x: u64) -> u64 { let _s = "a == b"; x ^ 3 }\n'
        m2 = list(_mutate_fn_body(rs2, "hotfn"))
        assert m2 and all('"a == b"' in m for m in m2), m2

        # micro_spec must retarget BOTH bench and profile examples (Q3 samples the
        # binary named by profile.example — the parent name is never built there)
        ms = _pf.micro_spec(pfspec, "hotfn", probe_rel)
        assert ms.bench["example"] == ms.profile["example"] == \
            _pf._example_name("probetest", "hotfn"), (ms.bench, ms.profile)
        assert ms.bench["probe"] == probe_rel and pfspec.bench["probe"] == "p.rs"
    ppath.unlink(missing_ok=True)
    print("#28 OK: probe rescue — coverage gate, qualify gates, freeze-before-generate, parent gate, honest failures")

    # --- #29: permtree — the cross-run exhaustion ledger --------------------------------
    import importlib
    import os as _os
    with tempfile.TemporaryDirectory() as d:
        _os.environ["ARO_PERMTREE_DIR"] = d
        from aro import permtree as _pt
        importlib.reload(_pt)
        try:
            e1 = Edit("src/a.rs", "x", "y")
            bs0 = _pt.baseline_state([])
            bs1 = _pt.baseline_state([e1])
            assert bs0 == "origin" and bs1 != bs0
            assert _pt.baseline_state([e1]) == bs1          # stable fingerprint

            _pt.record("demo", workload="demo", fn="sload", base_state=bs0,
                       verdict="noise-limited", regime="byte-identical", pct=5.7,
                       events_ref="out#a1", run_id="R1")
            _pt.record("demo", workload="demo", fn="sstore", base_state=bs0,
                       verdict="accepted", regime="byte-identical", delta=-19.2,
                       events_ref="out#a2", run_id="R1")
            # the sload node is RESCUED in a later run — same key, new state
            _pt.record("demo", workload="demo", fn="sload", base_state=bs0,
                       verdict="accepted", regime="micro-proven", delta=-4.5,
                       parent_delta=-0.4, probe_sha="9f2c", events_ref="out#a3",
                       run_id="R2")
            ns = _pt.nodes("demo")
            assert len(ns) == 2, ns
            sload = ns[_pt.node_key("demo", "sload", bs0)]
            assert sload["visits"] == 2 and sload["regime"] == "micro-proven"
            assert sload["parent_delta"] == -0.4 and sload["probe_sha"] == "9f2c"

            # closure: rescue closed the measurement-floor boundary for sload
            c = _pt.closure("demo", floor_pct=53.0, headroom_pct=1.5)
            b1, b2, b3 = c["boundaries"]
            assert b1["closed"] and b2["closed"] and not b3["closed"], c
            assert b2["rescued"] == ["sload"] and not c["exhausted"]
            c2 = _pt.closure("demo", floor_pct=53.0, headroom_pct=1.5,
                             workload_factory_state="dry")
            assert c2["exhausted"] is True
            # an OPEN case keeps boundary 2 open
            _pt.record("demo", workload="demo", fn="check_limit", base_state=bs1,
                       verdict="noise-limited", regime="byte-identical",
                       events_ref="out#a4", run_id="R2")
            c3 = _pt.closure("demo", floor_pct=53.0, headroom_pct=1.5,
                             workload_factory_state="dry")
            assert not c3["exhausted"] and c3["boundaries"][1]["open_cases"] == ["check_limit"]
        finally:
            del _os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    # union: cross-ledger merge, fn matrix, per-lane realized, global open debt
    with tempfile.TemporaryDirectory() as d2:
        _os.environ["ARO_PERMTREE_DIR"] = d2
        importlib.reload(_pt)
        try:
            _pt.record("wl-a", workload="wl-a", fn="hot", base_state="origin",
                       verdict="accepted", regime="byte-identical", delta=-10.0, pct=20.0)
            _pt.record("wl-a", workload="wl-a", fn="cold", base_state="origin",
                       verdict="noise-limited", regime="byte-identical", pct=2.0)
            _pt.record("wl-b", workload="wl-b", fn="hot", base_state="origin",
                       verdict="within-noise", regime="byte-identical", pct=5.0)
            assert _pt.ledgers() == ["wl-a", "wl-b"]
            u = _pt.union()
            assert set(u["lanes"]) == {"wl-a", "wl-b"}
            assert set(u["fn_matrix"]["hot"]) == {"wl-a", "wl-b"}   # side-by-side
            assert u["realized"]["wl-a"] == 10.0 and u["realized"]["wl-b"] == 0.0
            assert [c["fn"] for c in u["open_cases"]] == ["cold"]
            from aro import union as _un
            html = _un.render(u)
            assert '"wl-b"' in html and "window.__ARO_UNION__" not in html
        finally:
            del _os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#33 OK: permtree union — cross-ledger lanes, fn matrix, realized, open debt")

    print("#29 OK: permtree — stable node ids, last-state-wins, visits, exhaustion closure")

    # --- #34: frontier residue → ledger (seen but never tried) ---------------------------
    with tempfile.TemporaryDirectory() as d3:
        _os.environ["ARO_PERMTREE_DIR"] = d3
        importlib.reload(_pt)
        try:
            from aro import attempt as _atm

            class _REv:
                def __init__(self): self.events = []; self.context = {}
                def emit(self, ev, **f): self.events.append((ev, f))

            class _RSpec:
                name = "resid-test"

            # prior ledger state: an OPEN case + a judged accept
            _pt.record("resid-test", workload="resid-test", fn="old_pending",
                       base_state="origin", verdict="noise-limited",
                       regime="byte-identical", pct=3.0, events_ref="out#a1")
            _pt.record("resid-test", workload="resid-test", fn="done",
                       base_state="origin", verdict="accepted",
                       regime="byte-identical", delta=-4.0, events_ref="out#a2")
            buckets = {
                "untried": [{"name": "hot_new", "pct": 5.0, "symbol": ""},
                            {"name": "old_pending", "pct": 3.0, "symbol": ""},
                            {"name": "hot_attempted", "pct": 2.8, "symbol": ""}],
                "tried": [{"name": "warm", "pct": 2.5, "symbol": "", "verdict": "within-noise"}],
                "gated": [{"name": "arch_fn", "pct": 4.0, "symbol": "", "verdict": "scope-limit"}],
                "not_ours": [{"name": "memcpy", "pct": 30.0, "owner": "libc", "why": "runtime"}]}
            ev = _REv()
            n = _atm._record_residue("resid-test", _RSpec(), buckets,
                                     {"hot_attempted": 1}, [], Path(d3), ev,
                                     "budget spent (8)")
            assert n == 3, n                       # hot_new, warm, arch_fn — nothing else
            latest = {}
            for r in _pt.load("resid-test"):
                latest[r["fn"]] = r
            # the open case is NOT shadowed; attempted/judged fns get no residue row
            assert latest["old_pending"]["verdict"] == "noise-limited"
            assert latest["done"]["verdict"] == "accepted"
            assert "hot_attempted" not in latest and "memcpy" not in latest
            assert latest["hot_new"]["verdict"] == "no-attempt" \
                and latest["hot_new"]["regime"] == "unattempted" \
                and latest["hot_new"]["pct"] == 5.0 \
                and "budget spent" in latest["hot_new"]["hypothesis"]
            assert latest["warm"]["verdict"] == "no-attempt"
            assert latest["arch_fn"]["verdict"] == "gated" \
                and "scope-limit" in latest["arch_fn"]["hypothesis"]
            assert dict(ev.events)["frontier_residue"]["recorded"] == 3
            # union surfaces the residue; the open case stays the only debt
            u = _pt.union()
            assert u["fn_matrix"]["hot_new"]["resid-test"]["verdict"] == "no-attempt"
            assert [c["fn"] for c in u["open_cases"]] == ["old_pending"]
            # idempotent: a second stop records nothing new (all seen now)
            assert _atm._record_residue("resid-test", _RSpec(), buckets,
                                        {"hot_attempted": 1}, [], Path(d3), ev,
                                        "budget spent (8)") == 0
        finally:
            del _os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#34 OK: frontier residue — never-tried fns land in the ledger, open cases never shadowed")

    # --- #30: L4b workload factory — W1..W4 gates + the campaign closure chain ----------
    from aro import workload_factory as _wf
    from aro import attempt as _atmod
    import shutil as _sh

    wspec_base = _specmod.from_dict({
        "name": "wcamp-test", "target_repo": {"path": "."}, "metric": "ns",
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "k"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]}})
    pr, dr = _wf.workload_paths("wcamp-test", "v1")
    for rel in (pr, dr):
        fp = Path(_wf.REPO_ROOT) / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("// canned workload probe")
    try:
        base_hooks = dict(run_diff=lambda w: "DIFF aaaa",
                          mutate_diff=lambda w, k: (3, 3),
                          # base spec profiles only old_fn; the variant surfaces new_fn —
                          # campaign seeds `covered` from the BASE profile (W3 honesty)
                          profile_fns=lambda w: (["old_fn"] if w.name == "wcamp-test"
                                                 else ["new_fn", "old_fn"]))
        # (a) all gates pass
        q = _wf.qualify(wspec_base, "v1", pr, dr, covered_fns={"old_fn"}, **base_hooks)
        assert q.ok and q.probe_sha and q.diff_sha and q.new_fns == ["new_fn"], q
        # (b) W1: non-deterministic oracle
        flip = iter(["DIFF a", "DIFF b"])
        q = _wf.qualify(wspec_base, "v1", pr, dr, covered_fns=set(),
                        **{**base_hooks, "run_diff": lambda w: next(flip)})
        assert not q.ok and any("W1" in r for r in q.reasons), q.reasons
        # (c) W2: 2/3 mutations alarmed — all must
        q = _wf.qualify(wspec_base, "v1", pr, dr, covered_fns=set(),
                        **{**base_hooks, "mutate_diff": lambda w, k: (2, 3)})
        assert not q.ok and any("W2" in r for r in q.reasons), q.reasons
        # (d) W3: no frontier mass
        q = _wf.qualify(wspec_base, "v1", pr, dr, covered_fns={"new_fn", "old_fn"},
                        **base_hooks)
        assert not q.ok and any("W3" in r for r in q.reasons), q.reasons

        # campaign chain, three scenarios:
        #  (A) honest dry: author delivers, later proposals fail W3 → state "dry"
        #  (B) author retry: the FIRST author call fails transiently, the retry
        #      succeeds → the variant still qualifies and walks
        #  (C) persistent author failure: infrastructure error must NOT close
        #      boundary 3 as "dry" (the mega-evm-0703 `claude exited 143` lesson)
        calls = []
        def fake_attempt(spec_, **kw):
            calls.append((spec_.name, kw.get("workload_regime")))
            fn = "base_fn" if not calls[1:] else f"w_fn{len(calls)}"
            return ([{"name": fn, "pct": 5.0, "verdict": "within-noise",
                      "delta": None, "files": ["src/x.rs"], "regime":
                      kw.get("workload_regime") or "byte-identical"}], [])
        orig_attempt = _atmod.attempt
        _atmod.attempt = fake_attempt
        try:
            author_calls = {"n": 0}
            def flaky_author(spec_, wname, covered):
                author_calls["n"] += 1
                if author_calls["n"] == 1:
                    raise RuntimeError("transient LLM failure")   # retry covers it
                return pr, dr
            vprofile = {"n": 0}
            def draining_profile(w):
                if w.name == "wcamp-test":
                    return ["old_fn"]
                vprofile["n"] += 1
                # only the first variant surfaces new frontier mass; v2/v3 add
                # nothing → W3 rejects them → an HONEST dry chain
                return ["new_fn", "old_fn"] if vprofile["n"] <= 1 else ["old_fn"]
            with tempfile.TemporaryDirectory() as cd:
                all_rows, state = _atmod.campaign(
                    wspec_base, out_dir=Path(cd), events=_Ev(),
                    workload_proposals=5, dry_proposals=2,
                    workload_hooks={**base_hooks, "author": flaky_author,
                                    "profile_fns": draining_profile},
                    max_attempts=1, rounds_per_fn=1, min_pct=1.5, top=5)
            assert state == "dry", state
            assert len(all_rows) == 2 and "wcamp-test+v1" in all_rows, list(all_rows)
            assert calls[0] == ("wcamp-test", None)
            assert calls[1] == ("wcamp-test+v1", "synthetic-workload"), calls
            assert author_calls["n"] == 4, author_calls  # fail+retry, then v2, v3
            # (C) author dead for good → slots handed back, factory aborts, and the
            # state is an explicit author-error — never "dry"
            boom = {"n": 0}
            def dead_author(spec_, wname, covered):
                boom["n"] += 1
                raise RuntimeError("claude exited 143")
            with tempfile.TemporaryDirectory() as cd2:
                _rows2, state2 = _atmod.campaign(
                    wspec_base, out_dir=Path(cd2), events=_Ev(),
                    workload_proposals=5, dry_proposals=3,
                    workload_hooks={**base_hooks, "author": dead_author},
                    max_attempts=1, rounds_per_fn=1, min_pct=1.5, top=5)
            assert state2 == "author-error(2)", state2
            assert boom["n"] == 4, boom     # 2 slots x (try + retry)
        finally:
            _atmod.attempt = orig_attempt
        # the qualified variant was persisted for later campaigns
        saved = _wf.load_saved(wspec_base)
        assert saved and saved[0]["provenance"] == "synthetic-workload", saved
    finally:
        for rel in (pr, dr):
            (Path(_wf.REPO_ROOT) / rel).unlink(missing_ok=True)
        _sh.rmtree(Path(_wf.REPO_ROOT) / "targets" / "wcamp-test.workloads",
                   ignore_errors=True)
    print("#30 OK: workload factory — determinism/mutation/coverage gates + campaign dry-closure + synthetic provenance")


def case_23():
    # --- #31: llm.run_claude — the one claude invocation point, against a stub binary ----
    import stat as _stat
    from aro import llm as _llm
    with tempfile.TemporaryDirectory() as d:
        stub = Path(d) / "claude-stub"
        stub.write_text(
            "#!/bin/sh\n"
            "# echo argv so the test can assert flags; emit a claude-style JSON reply\n"
            'if [ "$1" = "--fail" ]; then echo boom >&2; exit 3; fi\n'
            "printf '%s' \'{\"result\": \"ok-reply\", \"usage\": {\"output_tokens\": 42}, \"total_cost_usd\": 0.5}\'\n")
        stub.chmod(stub.stat().st_mode | _stat.S_IEXEC)
        old_bin = _llm.CLAUDE_BIN
        _llm.CLAUDE_BIN = str(stub)
        try:
            text, toks, cost = _llm.run_claude("hi", timeout=10)
            assert text == "ok-reply" and toks == 42 and cost == 0.5, (text, toks, cost)
            # json_output=False returns raw stdout, no parsing
            raw, t0, c0 = _llm.run_claude("hi", timeout=10, json_output=False)
            assert "ok-reply" in raw and t0 == 0 and c0 == 0.0
            # non-zero exit → LLMError with the stderr tail
            _llm.CLAUDE_BIN = str(stub)
            failed = False
            try:
                # the stub reads $1; run_claude puts flags first — simulate failure by
                # a stub that always fails
                bad = Path(d) / "claude-bad"
                bad.write_text("#!/bin/sh\necho kaput >&2\nexit 7\n")
                bad.chmod(bad.stat().st_mode | _stat.S_IEXEC)
                _llm.CLAUDE_BIN = str(bad)
                _llm.run_claude("hi", timeout=10)
            except _llm.LLMError as e:
                failed = True
                assert "kaput" in str(e) and "7" in str(e), e
            assert failed, "non-zero exit must raise LLMError"
            # missing binary → LLMError (launch failure)
            _llm.CLAUDE_BIN = str(Path(d) / "no-such-binary")
            try:
                _llm.run_claude("hi", timeout=10)
                raise AssertionError("missing binary must raise LLMError")
            except _llm.LLMError:
                pass
        finally:
            _llm.CLAUDE_BIN = old_bin
    print("#31 OK: run_claude — json reply parsing, raw mode, LLMError on exit/launch failure")




def case_24():
    # --- #32: load-time artifact validation + polarity guard + plan defaults ---
    import json as _json
    import tempfile
    from aro import plan as _plan, spec as _spec
    base = {
        "name": "v", "target_repo": {"path": "/tmp/no-such-repo"},
        "hot_path": {"file": "src/lib.rs", "fn": "hot"},
        "metric": "ns_per_call",
        "benchmark_probe": {"pkg": "p", "example": "e",
                            "probe": "fixtures/mini-target/probes/mini_target.rs"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "constraints": {"editable": ["src"]},
    }
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.json"
        f.write_text(_json.dumps(base))
        sp = _spec.load(str(f))  # real probe file + non-empty regions → loads clean
        assert sp.regions == ["src"]
        # bench probe file missing → SpecError naming the slot at LOAD time
        bad = {**base, "benchmark_probe": {**base["benchmark_probe"],
                                           "probe": "probes/no-such-probe.rs"}}
        f.write_text(_json.dumps(bad))
        try:
            _spec.load(str(f))
            raise AssertionError("missing probe file must raise SpecError")
        except _spec.SpecError as e:
            assert "benchmark_probe.probe" in str(e), e
        # differential probe file missing → SpecError naming the slot
        bad2 = {**base, "correctness_oracle": {
            **base["correctness_oracle"],
            "differential": {"pkg": "p", "probe": "probes/no-such-diff.rs",
                             "example": "x", "prefix": "DIFF"}}}
        f.write_text(_json.dumps(bad2))
        try:
            _spec.load(str(f))
            raise AssertionError("missing diff probe file must raise SpecError")
        except _spec.SpecError as e:
            assert "differential.probe" in str(e), e
        # empty editable region must ERROR, not silently disable the guard
        bad3 = {**base, "hot_path": {}, "constraints": {}}
        f.write_text(_json.dumps(bad3))
        try:
            _spec.load(str(f))
            raise AssertionError("empty regions must raise SpecError")
        except _spec.SpecError as e:
            assert "editable" in str(e), e
    # polarity guard: count-like samples grow with the scale; per-op times do not
    assert _plan.polarity_suspect(100.0, 800.0, 8)       # 8x growth = a count
    assert not _plan.polarity_suspect(100.0, 105.0, 8)   # flat = per-op time
    assert not _plan.polarity_suspect(100.0, 60.0, 8)    # faster at scale: fine
    assert not _plan.polarity_suspect(None, 800.0, 8)    # missing leg: no verdict
    # plan defaults: whole-crate src editable via crate_rel; root crate → "src"
    asm = _plan.assemble_spec("p", Path("/tmp/r"), "abc123", "foo",
                              {"hot_path": {"file": "crates/foo/src/x.rs", "fn": "h"},
                               "has_diff": False}, crate_rel="crates/foo")
    assert asm["constraints"]["editable"] == ["crates/foo/src"]
    assert "differential" not in asm["correctness_oracle"]
    asm2 = _plan.assemble_spec("p", Path("/tmp/r"), "abc", "foo",
                               {"has_diff": False}, crate_rel=".")
    assert asm2["constraints"]["editable"] == ["src"]
    # cargo_args: normalized + type-checked; executable discovery from cargo JSON
    from aro import target as _target
    sp_args = _spec.from_dict({**base, "benchmark_probe": {
        **base["benchmark_probe"], "cargo_args": ["--features", "fast"]}})
    assert sp_args.bench["cargo_args"] == ["--features", "fast"]
    try:
        _spec.from_dict({**base, "benchmark_probe": {
            **base["benchmark_probe"], "cargo_args": "--features fast"}})
        raise AssertionError("string cargo_args must raise SpecError")
    except _spec.SpecError as e:
        assert "cargo_args" in str(e), e
    stream = "\n".join([
        "not json",
        '{"reason":"compiler-artifact","target":{"name":"other","kind":["example"]},"executable":"/t/other"}',
        '{"reason":"compiler-artifact","target":{"name":"probe","kind":["lib"]},"executable":null}',
        '{"reason":"compiler-artifact","target":{"name":"probe","kind":["example"]},"executable":"/t/release/examples/probe"}',
        '{"reason":"build-finished","success":true}'])
    assert _target._executable_from_cargo_json(stream, "probe") == "/t/release/examples/probe"
    assert _target._executable_from_cargo_json(stream, "nope") is None
    # guard scoping: a workspace member literally NAMED tests/benches is a crate,
    # not the harness — but real harness dirs and in-src unit-test modules stay locked
    from aro import guard as _guard
    assert _guard._screen_path("crates/x/tests/t.rs") is not None
    assert _guard._screen_path("src/tests/mod.rs") is not None
    assert _guard._screen_path("crates/tests/src/lib.rs") is None
    assert _guard._screen_path("benches/src/x.rs") is None
    # classify extras: spec-supplied ecosystem labels; never affects the ours decision
    from aro.symbols import classify_owner as _co
    tok_sym = "_ZN5tokio7runtime5spawn17h123456789abcdefE"
    assert _co(tok_sym, {"mycrate"})[0] == "unknown"
    assert _co(tok_sym, {"mycrate"}, extra={"runtime": ["tokio"]}) == ("runtime", "tokio")
    assert _co("_ZN4ring6digest17h1E", {"mycrate"}, extra={"crypto": ["ring"]})[0] == "crypto"
    assert _co(tok_sym, {"tokio"})[0] == "ours"   # ours always wins over extras
    # classify slot: validated at load
    sp_cls = _spec.from_dict({**base, "classify": {"runtime": ["tokio"]}})
    assert sp_cls.classify == {"runtime": ["tokio"]}
    try:
        _spec.from_dict({**base, "classify": {"runtime": "tokio"}})
        raise AssertionError("non-list classify must raise SpecError")
    except _spec.SpecError:
        pass
    # _owner_member: the profiled symbol's defining crate breaks same-name collisions
    from aro.frontier import _owner_member as _om
    sym = "_RNvNtCsAA_8mega_evm3evm7executeCsBB_16sweep_hotloop_v2"
    assert _om(["mega-evm", "state-test"], sym) == "mega-evm"
    assert _om(["state-test"], sym) is None
    # write_probe: autoexamples=false without an [[example]] stanza → actionable error
    sp_dot = _spec.from_dict({**base, "target_repo": {"path": "."}})
    tgt = _target.SpecTarget(sp_dot)
    with tempfile.TemporaryDirectory() as wd:
        wdir = Path(wd) / "k"; wdir.mkdir()
        (wdir / "Cargo.toml").write_text('[package]\nname = "k"\nautoexamples = false\n')
        try:
            tgt.write_probe(Path(wd), "k", "e")
            raise AssertionError("autoexamples=false must raise")
        except RuntimeError as e:
            assert "autoexamples" in str(e) and "[[example]]" in str(e), e
        (wdir / "Cargo.toml").write_text(
            '[package]\nname = "k"\nautoexamples = false\n[[example]]\nname = "e"\n')
        tgt.write_probe(Path(wd), "k", "e")
        assert (wdir / "examples" / "e.rs").exists()
    # bin-only preflight: pure check, actionable exit
    from aro.plan import require_lib_target as _rlt
    _rlt([{"name": "a", "dir": "/x", "kinds": ["lib", "bin"]}], "a")   # fine
    _rlt([{"name": "a", "dir": "/x"}], "a")                            # lenient: no kinds info
    try:
        _rlt([{"name": "a", "dir": "/x", "kinds": ["bin"]}], "a")
        raise AssertionError("bin-only crate must exit")
    except SystemExit as e:
        assert "library target" in str(e), e
    # lesson-gating: structured field first; narrow keywords; no verb/adjective traps
    import aro.frontier as _fr
    _old_recent = _fr.lessonsmod.recent
    _fr.lessonsmod.recent = lambda t, limit=200: [
        {"change": "a", "note": "layer-preserving macro arm, no layering change", "verdict": "within-noise"},
        {"change": "b", "note": "gated the rex5 check behind a flag", "verdict": "rejected"},
        {"change": "c", "note": "scope: accepted != should-merge, engineering cost", "verdict": "scope-limit"},
        {"change": "d", "note": "free text", "verdict": "ok", "gated": True},
        {"change": "e", "note": "reviewer liked it", "verdict": "ok", "gated": False},
    ]
    try:
        gflags = [g for (_, _, g) in _fr._lesson_index("t")]
        assert gflags == [False, False, True, True, False], gflags
    finally:
        _fr.lessonsmod.recent = _old_recent
    # word-boundary fn-name matching: `add` must not inherit "added ..." lessons
    idx = [("added a helper into the pipeline", "rejected", False),
           ("`add` carries a should-merge scope objection", "scope-limit", True),
           ("gated the rex5 check in `ret` behind a flag", "within-noise", False)]
    b = _fr.bucket_functions([("add", 5.0, "s1"), ("ret", 3.0, "s2"), ("dd", 2.0, "s3")],
                             "s", idx, 1.0)
    assert [r["name"] for r in b["gated"]] == ["add"], b["gated"]      # via the real row only
    assert [r["name"] for r in b["tried"]] == ["ret"], b["tried"]
    assert [r["name"] for r in b["untried"]] == ["dd"], b["untried"]   # "added" never matched
    print("#32 OK: spec load validates artifacts (probe files, editable regions) "
          "+ polarity guard + plan whole-crate defaults + cargo_args & executable discovery "
          "+ guard crate-named-tests scoping + classify extras + owner-member collisions "
          "+ autoexamples & bin-only preflight")


CASES = [case_01, case_02, case_03, case_04, case_05, case_06, case_07, case_08, case_09, case_11, case_12, case_14, case_15, case_16, case_17, case_18, case_19, case_20, case_21, case_22, case_23, case_24]


def run():
    """Run every case group; a failure no longer masks the rest — all failures
    are collected and reported, exit 1 if any."""
    import traceback
    failures = []
    for case in CASES:
        try:
            case()
        except Exception:
            failures.append((case.__name__, traceback.format_exc()))
    if failures:
        for name, tb in failures:
            print(f"\n=== FAILED {name} ===\n{tb}")
        raise SystemExit(f"SELFTEST FAILED: {len(failures)}/{len(CASES)} case group(s)")
    print("SELFTEST PASSED")

if __name__ == "__main__":
    run()
