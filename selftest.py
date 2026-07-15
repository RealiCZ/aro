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
        "llm_backend": "codex", "critic_backend": "grok",
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
    assert sp.llm_backend == "codex" and sp.critic_backend == "grok"
    # editable default = [hot_path.file] when constraints.editable absent
    sp2 = _spec.from_dict({**sd, "constraints": {}})
    assert sp2.regions == ["foo/src/x.rs"]
    # plan.assemble_spec emits a dict from_dict accepts
    asm = _plan.assemble_spec("p", Path("/tmp/r"), "HEAD", "foo",
                              {"hot_path": {"file": "foo/src/x.rs", "fn": "h"},
                               "metric": "ns", "direction": "minimize", "has_diff": True})
    sp3 = _spec.from_dict(asm)
    assert sp3.bench["pkg"] == "foo" and sp3.differential["example"] == "p_diff"
    assert sp3.llm_backend == "claude" and sp3.critic_backend is None
    print("#13 OK: 7-slot loader normalizes + LLM config + plan.assemble_spec round-trips")


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

    # pending-first: ledger open debts (noise-limited, no-attempt, no-candidate)
    # seed the queue AHEAD of the fresh untried frontier; resolved/closed do not
    from aro.frontier import _pending_names, _promote_pending
    ledger = [
        {"workload": "w", "fn": "b", "verdict": "noise-limited"},
        {"workload": "w", "fn": "c", "verdict": "no-attempt"},
        {"workload": "w", "fn": "a", "verdict": "accepted"},
        {"workload": "w", "fn": "d", "verdict": "noise-limited"},   # superseded below
        {"workload": "w", "fn": "d", "verdict": "within-noise"},    # latest wins → closed
        {"workload": "w", "fn": "g", "verdict": "no-candidate"},    # non-judgment → owed
        {"workload": "w", "fn": "nc", "verdict": "no-coverage"},    # probe miss → open debt
        {"workload": "other", "fn": "e", "verdict": "noise-limited"}]  # other workload
    pend = _pending_names(ledger, "w")
    assert pend == {"b", "c", "g"}, pend  # frontier pending set (no-coverage is permtree-only)
    # ...and permtree.open_debts agrees on the shared open set (+ no-coverage)
    from aro import permtree as _pt18
    owed18 = {d["fn"] for d in _pt18.open_debts(ledger) if d["workload"] == "w"}
    assert owed18 == {"b", "c", "g", "nc"}, owed18
    # no-coverage is OPEN (like no-candidate), not closed — surfaces for probe work
    assert "no-coverage" in _pt18._OPEN_VERDICTS
    assert "no-coverage" not in _pt18._CLOSED_VERDICTS
    # generator-down watch: K consecutive no-candidate headlines abort the walk;
    # anything judged (or even errored) in between breaks the chain
    from aro.attempt import _generator_down
    assert not _generator_down(["no-candidate", "no-candidate"])
    assert _generator_down(["accepted", "no-candidate", "no-candidate", "no-candidate"])
    assert not _generator_down(["no-candidate", "errored", "no-candidate"])
    q3 = _promote_pending(bk2, pend, tries={}, cap=2)
    assert [r["name"] for r in q3] == ["b", "c", "a"], q3   # debts first (9>3), then fresh
    # a promoted debt already at its try cap drops; fresh frontier respects the cap too
    q4 = _promote_pending(bk2, pend, tries={"b": 2, "a": 2}, cap=2)
    assert [r["name"] for r in q4] == ["c"], q4
    # a debt that fell off the current profile is not promoted (no longer addressable)
    q5 = _promote_pending(bk2, {"ghost"}, tries={}, cap=2)
    assert [r["name"] for r in q5] == ["a"], q5             # plain untried order
    # per-attempt seeded memory (the id-collision fix): cumulative resumes under unique ids
    from aro.types import Edit as _Ed
    with tempfile.TemporaryDirectory() as td2:
        m = _sw._seed_memory(Path(td2) / "a1", [_Ed("f.rs", "a", "b"), _Ed("g.rs", "c", "d")])
        ed = m.accepted_edits()
        assert [e.path for e in ed] == ["f.rs", "g.rs"], ed   # both, in order, no collision
    print("#18 OK: --attempt locate-grep + summarize + debt render + refill + pending-first + seeded-compound")



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
            # no-coverage is also OPEN: must surface via closure (probe debt),
            # not silently settle the measurement-floor boundary
            _pt.record("demo", workload="demo", fn="missed", base_state=bs1,
                       verdict="no-coverage", regime="strict",
                       events_ref="out#a5", run_id="R3")
            c4 = _pt.closure("demo", floor_pct=53.0, headroom_pct=1.5,
                             workload_factory_state="dry")
            assert not c4["exhausted"]
            assert set(c4["boundaries"][1]["open_cases"]) == {"check_limit", "missed"}
            assert "no-coverage" in _pt._OPEN_VERDICTS
            assert "no-coverage" not in _pt._CLOSED_VERDICTS
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
            assert u["conflicts"] == []          # within-noise is not a contradiction
            # lane B's re-visit REGRESSES what lane A accepted → the merge gate fires
            _pt.record("wl-b", workload="wl-b", fn="hot", base_state="origin",
                       verdict="regressed", regime="byte-identical", delta=2.1, pct=5.0)
            u = _pt.union()
            assert [c["fn"] for c in u["conflicts"]] == ["hot"], u["conflicts"]
            assert u["conflicts"][0]["verdicts"] == {"wl-a": "accepted",
                                                     "wl-b": "regressed"}
            from aro import union as _un
            html = _un.render(u)
            assert '"wl-b"' in html and "window.__ARO_UNION__" not in html
            assert "Merge gate" in html
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
                      kw.get("workload_regime") or "byte-identical"}], [],
                    "attempt budget spent (1)")
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
            # (D) generation agent hard-down on the BASE walk (quota-dead claude,
            # the rex5-01 lesson): the campaign must close author-error at once —
            # the factory's author runs through the SAME dead agent, so it is
            # never even called, and boundary 3 stays explicitly open
            def down_attempt(spec_, **kw):
                return ([{"name": "base_fn", "pct": 5.0, "verdict": "no-candidate",
                          "delta": None, "files": [], "regime": "byte-identical"}],
                        [], _atmod._GENERATOR_DOWN + ": 3 consecutive "
                        "zero-candidate attempts")
            _atmod.attempt = down_attempt
            never = {"n": 0}
            def untouchable_author(spec_, wname, covered):
                never["n"] += 1
                return pr, dr
            with tempfile.TemporaryDirectory() as cd3:
                _rows3, state3 = _atmod.campaign(
                    wspec_base, out_dir=Path(cd3), events=_Ev(),
                    workload_proposals=5, dry_proposals=3,
                    workload_hooks={**base_hooks, "author": untouchable_author},
                    max_attempts=1, rounds_per_fn=1, min_pct=1.5, top=5)
            assert state3 == "author-error(generator-down)", state3
            assert never["n"] == 0, never   # dead agent → factory never consulted
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
    # --- #31: run_llm + run_claude alias, at a mocked subprocess boundary ---
    from aro import llm as _llm
    calls = []
    state = {"behavior": "ok"}

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if state["behavior"] == "timeout":
            raise _llm.subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if state["behavior"] == "spawn":
            raise FileNotFoundError("no binary")
        if state["behavior"] == "nonzero":
            return _types.SimpleNamespace(stdout="", stderr="kaput", returncode=7)
        if state["behavior"] == "grok-degraded":
            return _types.SimpleNamespace(
                stdout='{"text":"unsafe","usage":{"output_tokens":1}}',
                stderr="warning: sandbox could not be applied, continuing without sandbox",
                returncode=0)
        if state["behavior"] == "grok-workspace-degraded":
            return _types.SimpleNamespace(
                stdout='{"text":"unsafe","usage":{"output_tokens":1}}',
                stderr="warning: sandbox initialization failed; defaulting to no sandbox",
                returncode=0)
        if "--output-format" not in cmd:
            return _types.SimpleNamespace(stdout="plain-reply", stderr="", returncode=0)
        return _types.SimpleNamespace(
            stdout=('{"result":"ok-reply","usage":{"output_tokens":42},'
                    '"total_cost_usd":0.5}'), stderr="", returncode=0)

    old_run = _llm.subprocess.run
    old_bin = _llm.CLAUDE_BIN
    old_grok_bin = _llm.GROK_BIN
    old_fallback = _llm.CLAUDE_FALLBACK_MODELS
    old_selected = _llm.os.environ.get("ARO_LLM_BACKEND")
    _llm.subprocess.run = fake_run
    _llm.CLAUDE_BIN = "claude-test"
    _llm.GROK_BIN = "grok-test"
    _llm.CLAUDE_FALLBACK_MODELS = "sonnet"
    try:
        text, toks, cost = _llm.run_llm("hi", backend="claude", timeout=10)
        assert (text, toks, cost) == ("ok-reply", 42, 0.5)
        argv, kwargs = calls[-1]
        assert argv[0] == "claude-test" and "--fallback-model" in argv, argv
        assert kwargs["timeout"] == 10 and kwargs["capture_output"] and kwargs["text"]

        # Compatibility alias is always Claude, independent of global selection.
        _llm.os.environ["ARO_LLM_BACKEND"] = "codex"
        assert _llm.run_claude("hi", timeout=10) == ("ok-reply", 42, 0.5)
        assert calls[-1][0][0] == "claude-test", calls[-1][0]

        _llm.CLAUDE_FALLBACK_MODELS = ""
        _llm.run_claude("hi", timeout=10)
        assert "--fallback-model" not in calls[-1][0]
        raw, t0, c0 = _llm.run_claude("hi", timeout=10, json_output=False)
        assert (raw, t0, c0) == ("plain-reply", 0, 0.0)
        assert "--output-format" not in calls[-1][0]

        state["behavior"] = "grok-degraded"
        try:
            _llm.run_llm("hi", backend="grok", timeout=10, allow_write=False)
            raise AssertionError("grok read-only sandbox degradation must raise")
        except _llm.LLMError as e:
            assert "read-only sandbox" in str(e), e

        state["behavior"] = "grok-workspace-degraded"
        try:
            _llm.run_llm("hi", backend="grok", timeout=10, allow_write=True)
            raise AssertionError("grok workspace sandbox degradation must raise")
        except _llm.LLMError as e:
            assert "workspace sandbox" in str(e), e

        for behavior, needle in (("nonzero", "kaput"), ("timeout", "timed out"),
                                 ("spawn", "failed to launch")):
            state["behavior"] = behavior
            try:
                _llm.run_llm("hi", backend="claude", timeout=10)
                raise AssertionError(f"{behavior} must raise LLMError")
            except _llm.LLMError as e:
                assert needle in str(e), e
    finally:
        _llm.subprocess.run = old_run
        _llm.CLAUDE_BIN = old_bin
        _llm.GROK_BIN = old_grok_bin
        _llm.CLAUDE_FALLBACK_MODELS = old_fallback
        _llm.os.environ.pop("ARO_LLM_BACKEND", None)
        if old_selected is not None:
            _llm.os.environ["ARO_LLM_BACKEND"] = old_selected
    print("#31 OK: run_llm + pinned run_claude alias; hermetic success/raw/error policy")




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
    with tempfile.TemporaryDirectory() as wd:
        # Keep SpecTarget's sibling CARGO_TARGET_DIR under the temp root too;
        # this case must remain hermetic in workspace-write sandboxes.
        work = Path(wd) / "repo"; work.mkdir()
        sp_dot = _spec.from_dict({**base, "target_repo": {"path": str(work)}})
        tgt = _target.SpecTarget(sp_dot)
        wdir = work / "k"; wdir.mkdir()
        (wdir / "Cargo.toml").write_text('[package]\nname = "k"\nautoexamples = false\n')
        try:
            tgt.write_probe(work, "k", "e")
            raise AssertionError("autoexamples=false must raise")
        except RuntimeError as e:
            assert "autoexamples" in str(e) and "[[example]]" in str(e), e
        (wdir / "Cargo.toml").write_text(
            '[package]\nname = "k"\nautoexamples = false\n[[example]]\nname = "e"\n')
        tgt.write_probe(work, "k", "e")
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
    # gating at lesson WRITE time: only a critic REJECT on an architectural rubric
    # gates; cheating/behaviour rubrics condemn the candidate, not the function
    from aro.attempt import _lesson_gated
    from aro.types import EvalOutcome as _EO, Verdict as _V
    assert _lesson_gated(_EO("c", _V.REJECTED, [], [],
                             critic_rubrics=["layer-dissolve"])) is True
    assert _lesson_gated(_EO("c", _V.REJECTED, [], [],
                             critic_rubrics=["conflate-responsibilities"])) is True
    assert _lesson_gated(_EO("c", _V.REJECTED, [], [],
                             critic_rubrics=["reward-hack"])) is False
    assert _lesson_gated(_EO("c", _V.REJECTED, [], [],
                             critic_rubrics=["dead-code-on-hunch", "correctness-suspicion"])) is False
    assert _lesson_gated(_EO("c", _V.REJECTED, [], [])) is False   # prescreen drop: no rubrics
    assert _lesson_gated(_EO("c", _V.ACCEPTED, [], [],
                             critic_rubrics=["layer-dissolve"])) is False  # only rejects gate
    # the write side lands the structured field; the read side then never sniffs it
    import aro.lessons as _lm
    with tempfile.TemporaryDirectory() as ld:
        _old_lpath = _lm._PATH
        _lm._PATH = Path(ld) / "lessons.jsonl"
        try:
            _lm.append("t", "inline the sstore ext", "rejected", None,
                       "gated the fast path", gated=True)
            _lm.append("t", "hoist a bound check", "within-noise", -0.1,
                       "architectural note in passing", gated=False)
            _lm.append("t", "legacy row", "ok")                    # None → field omitted
            rows = _lm.recent("t")
            assert rows[0]["gated"] is True and rows[1]["gated"] is False
            assert "gated" not in rows[2]
            _fr.lessonsmod.recent = lambda t, limit=200: rows
            gflags = [g for (_, _, g) in _fr._lesson_index("t")]
            # row 1 says "architectural" in its note but gated:false WINS over keywords
            assert gflags == [True, False, False], gflags
        finally:
            _lm._PATH = _old_lpath
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


def case_25():
    # --- #35: reject-archiving + `aro clean` (scan is the testable core) -----------------
    import importlib
    import os as _os
    import subprocess as _sp
    from aro import attempt as _atm
    from aro import clean as _cl
    from aro import workload_factory as _wf

    class _Ev:
        def __init__(self): self.events = []; self.context = {}
        def emit(self, ev, **f): self.events.append((ev, f))

    # (a) a rejected probe ARCHIVES into the run dir (never plain-deleted)
    rel = "probes/selftest-archive-w-v9.rs"
    src = Path(_wf.REPO_ROOT) / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("// doomed probe")
    try:
        with tempfile.TemporaryDirectory() as od:
            ev = _Ev()
            _atm._archive_rejected(Path(od), [rel], ev, reason="failed W3")
            moved = Path(od) / "rejected-probes" / Path(rel).name
            assert moved.exists() and not src.exists()
            assert moved.read_text() == "// doomed probe"
            assert dict(ev.events)["probe_archived"]["probe"] == rel
            # a missing source is a silent no-op (author died before writing)
            ev2 = _Ev()
            _atm._archive_rejected(Path(od), ["probes/never-written.rs"], ev2, reason="x")
            assert ev2.events == []
    finally:
        src.unlink(missing_ok=True)

    # (b) clean.scan: registered worktrees kept, orphans + their td dirs found,
    #     ledger-referenced run dirs protected
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        repo = root / "repo"
        repo.mkdir()
        _sp.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "f.txt").write_text("x")
        _sp.run(["git", "add", "."], cwd=repo, check=True)
        _sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "i"],
                cwd=repo, check=True)
        wtp = root / ".aro-worktrees"
        wtp.mkdir()
        _sp.run(["git", "worktree", "add", "--detach", "-q", str(wtp / "live-1"), "HEAD"],
                cwd=repo, check=True)
        (wtp / "orphan-2").mkdir()                       # unregistered leftover
        td = root / ".aro-demo-td"
        (td / "live-1").mkdir(parents=True)              # backs a kept worktree
        (td / "orphan-2").mkdir()                        # backs a doomed worktree
        (td / "gone-3").mkdir()                          # worktree dir already gone
        runs = root / "runs"
        (runs / "run-kept").mkdir(parents=True)
        (runs / "run-old").mkdir()
        with tempfile.TemporaryDirectory() as pd:
            _os.environ["ARO_PERMTREE_DIR"] = pd
            from aro import permtree as _pt2
            importlib.reload(_pt2)
            importlib.reload(_cl)
            try:
                _pt2.record("demo", workload="demo", fn="f", base_state="origin",
                            verdict="accepted", regime="byte-identical",
                            events_ref=f"{runs}/run-kept#a1")
                found = _cl.scan(repo, "demo", runs_dir=runs)
                assert [p.name for p in found["kept_live"]] == ["live-1"]
                assert [p.name for p in found["worktrees"]] == ["orphan-2"]
                assert sorted(p.name for p in found["tds"]) == ["gone-3", "orphan-2"]
                assert [p.name for p in found["runs"]] == ["run-old"]
                assert [p.name for p in found["runs_protected"]] == ["run-kept"]
                # --registered claims the registered worktree too (crash cleanup)
                found2 = _cl.scan(repo, "demo", registered=True)
                assert sorted(p.name for p in found2["worktrees"]) == ["live-1", "orphan-2"]
            finally:
                del _os.environ["ARO_PERMTREE_DIR"]
                importlib.reload(_pt2)
                importlib.reload(_cl)
    print("#35 OK: reject-archiving into the run dir + clean.scan (live kept, orphans "
          "found, ledger-referenced runs protected)")


def case_26():
    # --- #36: recheck — the computed re-run signal after the target repo moves ----------
    import subprocess as _sp
    from aro import recheck as _rc

    def _git(repo, *a):
        _sp.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *a],
                check=True, capture_output=True)

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "src").mkdir()
        (repo / "src" / "hot.rs").write_text("fn hot() {}")
        (repo / "README.md").write_text("v1")
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "c1")
        c1 = _sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                     capture_output=True, text=True).stdout.strip()

        repo_s = str(repo)

        class _S:
            repo = repo_s
            baseline_ref = c1
            regions = ["src"]

        # baseline IS the head
        assert _rc.assess(_S())["verdict"] == "current"
        # head moved, but only OUTSIDE the editable regions → claim stands
        (repo / "README.md").write_text("v2")
        _git(repo, "commit", "-aqm", "c2")
        a = _rc.assess(_S())
        assert a["verdict"] == "still-current" and a["other_churn"] == ["README.md"] \
            and a["ahead"] == 1, a
        # churn lands under the regions → the judged code is gone: RE-RUN
        (repo / "src" / "hot.rs").write_text("fn hot() { faster() }")
        _git(repo, "commit", "-aqm", "c3")
        a = _rc.assess(_S())
        assert a["verdict"] == "re-run" and a["region_churn"] == ["src/hot.rs"] \
            and a["ahead"] == 2, a
        # unresolvable baseline / baseline not an ancestor → re-pin first
        class _S2(_S):
            baseline_ref = "deadbeef"
        assert _rc.assess(_S2())["verdict"] == "re-pin"

        class _S3(_S):
            baseline_ref = "HEAD"
        assert _rc.assess(_S3(), ref=c1)["verdict"] == "re-pin"
    print("#36 OK: recheck — current / still-current / re-run / re-pin from real git churn")


def case_27():
    # --- #37: coverage — dark regions from a merged llvm-cov export (pure parse) --------
    import json as _json
    from aro import coverage as _cov
    from aro import workload_factory as _wf2

    wt = "/wt/cov-123"
    export = {"data": [{
        "files": [
            {"filename": f"{wt}/src/evm.rs",
             "summary": {"functions": {"count": 4, "covered": 2},
                         "lines": {"percent": 55.0}}},
            {"filename": f"{wt}/src/lit.rs",
             "summary": {"functions": {"count": 2, "covered": 2},
                         "lines": {"percent": 100.0}}},
            {"filename": f"{wt}/crates/p/examples/probe.rs",     # the probe itself
             "summary": {"functions": {"count": 1, "covered": 1},
                         "lines": {"percent": 100.0}}},
            {"filename": "/Users/x/.cargo/registry/dep/src/lib.rs",  # a dependency
             "summary": {"functions": {"count": 9, "covered": 0},
                         "lines": {"percent": 0.0}}}],
        "functions": [
            {"name": "_ZN4mini8dark_one17h0011223344556677E", "count": 0,
             "filenames": [f"{wt}/src/evm.rs"]},
            {"name": "_ZN4mini8dark_two17h8899aabbccddeeffE", "count": 0,
             "filenames": [f"{wt}/src/evm.rs"]},
            {"name": "_ZN4mini3lit17h0000000000000001E", "count": 812,
             "filenames": [f"{wt}/src/lit.rs"]},
            {"name": "probe_main", "count": 0,
             "filenames": [f"{wt}/crates/p/examples/probe.rs"]},   # excluded
            {"name": "dep_fn", "count": 0,
             "filenames": ["/Users/x/.cargo/registry/dep/src/lib.rs"]}]}]}  # excluded
    g = _cov.dark_regions(export, wt, our_token="mini")
    assert [f["file"] for f in g["files"]] == ["src/evm.rs", "src/lit.rs"], g["files"]
    assert g["files"][0]["dark_fns"] == 2 and g["files"][1]["dark_fns"] == 0
    assert [d["fn"] for d in g["dark_fns"]] == ["dark_one", "dark_two"], g["dark_fns"]
    assert all(d["file"] == "src/evm.rs" for d in g["dark_fns"])
    assert g["totals"] == {"functions": 6, "covered": 4, "dark": 2,
                           "covered_pct": 66.7}, g["totals"]

    # the workload author's prompt fragment reads the artifact (and says so when absent)
    class _CSpec:
        name = "covgap-selftest"
    gp = _cov.gap_path("covgap-selftest")
    try:
        assert "no coverage-gap report" in _wf2._dark_context(_CSpec())
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(_json.dumps(g))
        ctx = _wf2._dark_context(_CSpec())
        assert "dark_one" in ctx and "src/evm.rs" in ctx and "Dark regions" in ctx
        gp.write_text(_json.dumps({"dark_fns": []}))
        assert "no dark functions" in _wf2._dark_context(_CSpec())
    finally:
        gp.unlink(missing_ok=True)
    # base workload always registered; saved factory workloads follow
    class _CSpec2:
        name = "covgap-selftest"
        bench = {"example": "base_probe", "probe": "probes/x.rs", "pkg": "p"}
    assert _cov.registered_workloads(_CSpec2()) == [("base_probe", "probes/x.rs")]
    print("#37 OK: coverage — dark regions parsed from merged export (deps + probes "
          "excluded), factory prompt reads the artifact")

    # serve --port default honors ARO_SERVE_PORT (set once per box)
    import importlib
    import os as _os2
    import aro.cli as _cli
    _os2.environ["ARO_SERVE_PORT"] = "8100"
    try:
        importlib.reload(_cli)
        a = _cli.build_parser().parse_args(["serve", "/x"])
        assert a.port == 8100, a.port
    finally:
        del _os2.environ["ARO_SERVE_PORT"]
        importlib.reload(_cli)
    assert _cli.build_parser().parse_args(["serve", "/x"]).port == 8010
    print("#38 OK: serve port default honors ARO_SERVE_PORT")


def case_28():
    # --- #39: aro next — the whole state machine, every action + anti-loop rule ---------
    import importlib
    import os as _os
    from aro import next as _nx

    base = {"spec": "s", "has_ledger": True, "debts": [], "debt_keys": [],
            "campaign_state": {"state": "dry", "out_dir": "/r/out",
                               "debts_open": []},
            "manifest": {"accepted": 0, "mergeable": 0},
            "recheck": {"verdict": "still-current"},
            "coverage_dark": 0, "coverage_stale": False, "conflicts": []}

    def d(**over):
        return _nx.decide({**base, **over})

    # liveness guards outrank EVERYTHING, including ignite-first — a live or
    # crashed run means every other recorded signal is untrustworthy
    assert d(live_run=True, has_ledger=False, campaign_state={})["action"] == "wait"
    assert d(crashed_run=True, has_ledger=False,
             campaign_state={})["action"] == "mark-interrupted"

    # regression: after --mark interrupted (state=author-error(interrupted)),
    # a crashed campaign whose debt set is UNCHANGED (debts_open preserved by
    # the non-destructive ignition marker) must fall THROUGH pay-debts to
    # retry-factory — the anti-loop floor still holds. If the ignition marker
    # had blanked debts_open, debt_keys != None would wrongly re-drive an
    # expensive pay-debts sweep over the probe-capped floor.
    r = d(campaign_state={"state": "author-error(interrupted)",
                          "out_dir": "/r/out", "debts_open": ["w·f"]},
          debts=[{"workload": "w", "fn": "f", "verdict": "noise-limited"}],
          debt_keys=["w·f"],
          manifest={"accepted": 0, "mergeable": 0})
    assert r["action"] == "retry-factory", r
    assert any("probe-capped" in w for w in r["warnings"]), r
    # contrast: a CHANGED debt set after interrupt is real work → pay-debts
    assert d(campaign_state={"state": "author-error(interrupted)",
                             "out_dir": "/r/out", "debts_open": []},
             debts=[{"workload": "w", "fn": "f", "verdict": "noise-limited"}],
             debt_keys=["w·f"],
             manifest={"accepted": 0, "mergeable": 0})["action"] == "pay-debts"

    # the ladder, top to bottom — each guard reachable, first match wins
    assert d(has_ledger=False, campaign_state={})["action"] == "ignite-first"
    assert d(recheck={"verdict": "re-pin", "reason": "x"})["action"] == "re-pin"
    assert d(manifest=None)["action"] == "rebuild-manifest"
    assert d(manifest={"accepted": 2, "mergeable": 1})["action"] == "harvest"
    st_h = {**base["campaign_state"], "harvested": True}
    assert d(manifest={"accepted": 2, "mergeable": 1},
             campaign_state=st_h)["action"] != "harvest"       # marked → advances
    assert d(recheck={"verdict": "re-run", "region_churn": ["src/a.rs"]}
             )["action"] == "re-run"
    assert d(debts=[{"workload": "w", "fn": "f", "verdict": "noise-limited"}],
             debt_keys=["w·f"])["action"] == "pay-debts"        # NEW debt set
    # anti-loop: the same debt set the last campaign left → floor, fall through
    r = d(debts=[{"workload": "w", "fn": "f", "verdict": "noise-limited"}],
          debt_keys=["w·f"],
          campaign_state={**base["campaign_state"], "debts_open": ["w·f"]})
    assert r["action"] == "watch" and any("probe-capped" in w for w in r["warnings"]), r
    assert d(campaign_state={**base["campaign_state"], "state": "author-error(2)"}
             )["action"] == "retry-factory"
    assert d(coverage_dark=None)["action"] == "coverage"        # no report
    assert d(coverage_stale=True)["action"] == "coverage"       # report predates run
    assert d(coverage_dark=3)["action"] == "light-dark-regions"
    assert d()["action"] == "watch"                             # everything closed
    # warnings ride on every action: conflicts + blind recheck
    r = d(conflicts=[{"fn": "hot", "verdicts": {"a": "accepted", "b": "regressed"}}],
          recheck={"verdict": "unknown", "reason": "no repo"})
    assert r["action"] == "watch" and len(r["warnings"]) == 2, r
    assert any("hot" in w for w in r["warnings"])
    assert any("blind" in w for w in r["warnings"])

    # gather + state round-trip on a scratch permtree dir (recheck degrades to
    # unknown on an unreachable repo — a fact, not an error)
    import tempfile as _tf
    with _tf.TemporaryDirectory() as pd:
        _os.environ["ARO_PERMTREE_DIR"] = pd
        from aro import permtree as _pt3
        importlib.reload(_pt3)
        importlib.reload(_nx)
        try:
            class _NSpec:
                name = "next-selftest"
                repo = "/no/such/repo"
                baseline_ref = "HEAD"
                regions = ["src"]

            _pt3.record("next-selftest", workload="next-selftest", fn="f",
                        base_state="origin", verdict="noise-limited",
                        regime="byte-identical", events_ref="/r/out#a1")
            _pt3.record_state("next-selftest", state="dry", out_dir="/r/out",
                              debts_open=[])
            s = _nx.gather(_NSpec())
            assert s["has_ledger"] and s["debt_keys"] == ["next-selftest·f"]
            # a missing repo means the baseline cannot resolve → re-pin outranks all
            assert s["recheck"]["verdict"] == "re-pin"
            assert _nx.decide(s)["action"] == "re-pin"
            _pt3.mark_state("next-selftest", harvested=True)
            assert _pt3.load_state("next-selftest")["harvested"] is True
            assert _pt3.load_state("next-selftest")["state"] == "dry"
            assert _pt3.ledgers() == ["next-selftest"]   # .state.json is not a ledger

            # liveness: a real (self) pid reads live; a reaped subprocess pid
            # reads crashed — _pid_alive is the only OS-touching seam here
            import subprocess as _sp
            assert _nx._pid_alive(_os.getpid()) is True
            dead = _sp.Popen(["true"])
            dead.wait()
            assert _nx._pid_alive(dead.pid) is False
            assert _nx._pid_alive("not-an-int") is False
            # EPERM proves the process exists (owned by another user) → alive,
            # NOT crashed — else the oracle re-ignites over a live run
            _real_kill = _os.kill
            _os.kill = lambda *_a: (_ for _ in ()).throw(PermissionError())
            try:
                assert _nx._pid_alive(999999) is True
            finally:
                _os.kill = _real_kill

            # the ignition marker MERGES a running_pid sidecar (aro/sweep.py) —
            # it must NOT clobber the prior closure. Seed a closure with an
            # open debt set, then merge the sidecar and confirm debts_open
            # survives underneath the liveness fields.
            _pt3.record_state("next-selftest", state="dry", out_dir="/r/old",
                              debts_open=["next-selftest·f"])
            _pt3.mark_state("next-selftest", running_pid=_os.getpid(),
                            running_out_dir="/r/out", running_since="t0")
            st_live = _pt3.load_state("next-selftest")
            assert st_live["debts_open"] == ["next-selftest·f"]   # closure kept
            assert st_live["state"] == "dry"                      # not overwritten
            s_live = _nx.gather(_NSpec())
            assert s_live["live_run"] and not s_live["crashed_run"]
            assert _nx.decide(s_live)["action"] == "wait"
            assert _nx.decide(s_live)["command"] == ""            # nothing to run

            _pt3.mark_state("next-selftest", running_pid=dead.pid)
            s_dead = _nx.gather(_NSpec())
            assert s_dead["crashed_run"] and not s_dead["live_run"]
            assert _nx.decide(s_dead)["action"] == "mark-interrupted"

            # cli() --mark interrupted clears the sidecar + sets the
            # author-error(...) family, and LEAVES debts_open intact so the
            # anti-loop floor still holds; an unrecognized mark is a hard error
            from aro import spec as _specmod
            orig_load = _specmod.load
            _specmod.load = lambda path: _NSpec()
            try:
                _nx.cli(_types.SimpleNamespace(spec="next-selftest",
                                               mark="interrupted", json=False))
                st_marked = _pt3.load_state("next-selftest")
                assert st_marked["state"] == "author-error(interrupted)"
                assert st_marked["running_pid"] is None            # sidecar cleared
                assert st_marked["debts_open"] == ["next-selftest·f"]  # floor kept
                s_after = _nx.gather(_NSpec())
                assert not s_after["live_run"] and not s_after["crashed_run"]
                try:
                    _nx.cli(_types.SimpleNamespace(spec="next-selftest",
                                                   mark="bogus", json=False))
                    raise AssertionError("expected SystemExit on unknown --mark")
                except SystemExit:
                    pass
            finally:
                _specmod.load = orig_load
        finally:
            del _os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt3)
            importlib.reload(_nx)
    print("#39 OK: aro next — full ladder reachable, anti-loop floors, warnings ride along")


def case_29():
    """#40: instruction-count gate — parser, profile guard, gate logic, records.
    Fully hermetic: fixture callgrind texts + injected Ir values; no valgrind."""
    import io
    import os
    from contextlib import redirect_stderr
    from aro import icount as _ic
    from aro import eval as _evalmod
    from aro import lessons as _les
    from aro import permtree as _pt
    from aro.types import (Candidate as _C, Edit as _Ed,
                           Objective as _Obj, Patch as _P, Verdict as _V)

    # --- parser: normal, omitted trailing zeros, malformed, missing totals ---
    normal = (
        "# callgrind format\nversion: 1\n"
        "creator: callgrind-3.26.0.codspeed5\n"
        "cmd:  ./target/release/examples/probe\n"
        "events: Ir Dr Dw I1mr D1mr D1mw ILmr DLmr DLmw sysCount sysTime sysCpuTime\n"
        "totals: 123456789 456 123 2 0 1 2 0 1\n"
    )
    t = _ic.parse_callgrind_totals(normal)
    assert t["Ir"] == 123456789 and t["Dr"] == 456
    # trailing columns omitted → 0
    assert t["sysCount"] == 0 and t["sysTime"] == 0 and t["sysCpuTime"] == 0
    short = (
        "events: Ir Dr Dw I1mr D1mr\n"
        "totals: 1000 10 5\n"  # last two columns omitted
    )
    t2 = _ic.parse_callgrind_totals(short)
    assert t2 == {"Ir": 1000, "Dr": 10, "Dw": 5, "I1mr": 0, "D1mr": 0}
    bad = "events: Ir Dr\ntotals: 100 notanumber\n"
    try:
        _ic.parse_callgrind_totals(bad)
        raise AssertionError("malformed token must raise")
    except ValueError as e:
        assert "malformed" in str(e).lower() or "notanumber" in str(e)
    try:
        _ic.parse_callgrind_totals("events: Ir Dr\n# no totals\n")
        raise AssertionError("missing totals must raise")
    except ValueError as e:
        assert "totals" in str(e).lower()
    print("#40a OK: callgrind parser (normal / trailing-zero / malformed / missing)")

    # --- profile fidelity guard (tempdir Cargo.toml fixtures) ---
    clean = '[package]\nname = "x"\n\n[profile.release]\nopt-level = 3\nlto = "thin"\n'
    assert _ic.check_profile_fidelity(clean) is None
    bench_cgu = clean + "\n[profile.bench]\ncodegen-units = 1\n"
    err = _ic.check_profile_fidelity(bench_cgu)
    assert err and "profile.bench" in err and "codegen-units" in err, err
    bench_lto = clean + "\n[profile.bench]\nlto = true\n"
    err = _ic.check_profile_fidelity(bench_lto)
    assert err and "profile.bench" in err and "lto" in err, err
    rel_cgu1 = '[package]\nname = "x"\n\n[profile.release]\ncodegen-units = 1\n'
    err = _ic.check_profile_fidelity(rel_cgu1)
    assert err and "profile.release" in err and "codegen-units" in err, err
    # [profile.maxperf] is NOT a measurement profile — must NOT reject
    maxperf = clean + "\n[profile.maxperf]\ncodegen-units = 1\nlto = true\n"
    assert _ic.check_profile_fidelity(maxperf) is None
    fp1 = _ic.profile_fingerprint(clean, "rustc 1.80.0")
    fp2 = _ic.profile_fingerprint(clean, "rustc 1.80.0")
    fp3 = _ic.profile_fingerprint(clean + "\n", "rustc 1.81.0")
    assert fp1 == fp2 and fp1.startswith("rustc 1.80.0|")
    assert fp1 != fp3  # rustc version is part of the fingerprint
    print("#40b OK: profile fidelity guard + fingerprint")

    # --- gate logic: injected Ir pairs for every terminal verdict + ε + locality ---
    def _r(ir, d1mr=0, dlmr=0, fp="rustc|abc"):
        return _ic.ICountResult(ir=ir, events={"Ir": ir, "D1mr": d1mr, "DLmr": dlmr},
                                profile_fingerprint=fp)

    # ACCEPTED_IR: cand much smaller Ir
    d = _ic.judge_ir(_r(10000), _r(9000), epsilon_pct=0.1, locality=False)
    assert not d.passthrough and d.verdict == _V.ACCEPTED_IR and d.ir_delta_pct == -10.0
    # REGRESSED_IR
    d = _ic.judge_ir(_r(10000), _r(11000), epsilon_pct=0.1, locality=False)
    assert d.verdict == _V.REGRESSED_IR and d.ir_delta_pct == 10.0
    # NEUTRAL_IR within ε (cpu)
    d = _ic.judge_ir(_r(10000), _r(10005), epsilon_pct=0.1, locality=False)
    assert d.verdict == _V.NEUTRAL_IR and abs(d.ir_delta_pct) <= 0.1
    # ε boundary: exactly −ε is NOT improvement (need Δ < −ε)
    d = _ic.judge_ir(_r(10000), _r(9990), epsilon_pct=0.1, locality=False)  # −0.1%
    assert d.verdict == _V.NEUTRAL_IR, d
    d = _ic.judge_ir(_r(10000), _r(9989), epsilon_pct=0.1, locality=False)  # −0.11%
    assert d.verdict == _V.ACCEPTED_IR, d
    # locality + cache evidence → passthrough
    d = _ic.judge_ir(_r(10000, d1mr=100, dlmr=50),
                     _r(10005, d1mr=80, dlmr=40),
                     epsilon_pct=0.1, locality=True)
    assert d.passthrough and d.verdict is None
    # locality without cache evidence → NEUTRAL_IR
    d = _ic.judge_ir(_r(10000, d1mr=100, dlmr=50),
                     _r(10005, d1mr=100, dlmr=50),
                     epsilon_pct=0.1, locality=True)
    assert d.verdict == _V.NEUTRAL_IR
    # locality with cache WORSE → NEUTRAL_IR
    d = _ic.judge_ir(_r(10000, d1mr=100, dlmr=50),
                     _r(10005, d1mr=120, dlmr=40),
                     epsilon_pct=0.1, locality=True)
    assert d.verdict == _V.NEUTRAL_IR
    print("#40c OK: Ir gate verdicts (accepted/neutral/regressed/ε/locality)")

    # --- coverage precheck + evaluate() with injected icount ---
    assert _ic.probe_covers_patch(["crates/mega-evm/src/evm"],
                                  ["crates/mega-evm/src/evm/host.rs"])
    assert not _ic.probe_covers_patch(["crates/mega-evm/src/evm"],
                                      ["crates/other/src/foo.rs"])
    assert _ic.probe_covers_patch(["crates/x"], [])  # NoOp always covered

    class _IcountTarget(MockTarget):
        """MockTarget + icount that returns pre-seeded results by worktree tag."""
        def __init__(self, pairs, probe_covers=None, epsilon=0.1):
            super().__init__()
            self._pairs = pairs  # list of (base_r, cand_r) consumed in order
            self._idx = 0
            self.spec = _types.SimpleNamespace(
                probe_covers=list(probe_covers or []),
                icount_epsilon_pct=epsilon,
                raw={},
                timeout=30,
                name="mock-ic",
            )

        def icount(self, work, scale=1, cache_sim=False):
            # First call is baseline, second is candidate (per evaluate).
            # MockTarget worktrees are "/tmp/mock-wt-cand-..."; baseline_work is
            # the string "base" in evaluate tests.
            if str(work) == "base" or (isinstance(work, str) and work.startswith("base")):
                return self._pairs[self._idx][0]
            r = self._pairs[self._idx][1]
            self._idx = min(self._idx + 1, len(self._pairs) - 1)
            return r

    objs = [_Obj("metric/x", True)]
    floors = __import__("aro.types", fromlist=["NoiseFloors"]).NoiseFloors()
    floors.put("metric/x", 2.0)

    # Gate 1.5 now requires a selfcheck marker; this block tests Ir adjudication
    # only (selfcheck is case_33). Skip the host-health precondition here.
    # ARO_SKIP_SELFCHECK short-circuits BEFORE version probing so evaluate()
    # stays fully hermetic (no real subprocesses).
    import subprocess as _subprocess
    os.environ["ARO_SKIP_SELFCHECK"] = "1"
    _real_sp_run = _subprocess.run

    def _no_subprocess(*a, **k):
        raise AssertionError(
            "case_29 hermeticity broken: subprocess.run fired under "
            "ARO_SKIP_SELFCHECK (skip must short-circuit before probing)")

    _subprocess.run = _no_subprocess
    try:
        # ACCEPTED_IR via evaluate
        t = _IcountTarget([(_r(10000), _r(9000))],
                          probe_covers=["src/"])
        cand = _C(id="ir-win", hypothesis="drop a multiply",
                  patch=_P([_Ed("src/opt.rs", "a", "b")]))
        with redirect_stderr(io.StringIO()):
            out = _evalmod.evaluate(t, "base", _P([]), cand, 2, floors, objs,
                                    aa_runs=1, bench_scales=(1,))
        assert out.verdict == _V.ACCEPTED_IR, out.verdict
        assert out.ir_delta_pct == -10.0
        assert out.profile_fingerprint == "rustc|abc"
        assert out.deltas and out.deltas[0].metric == "Ir"
        # skip-when-absent: no env_fingerprint when selfcheck was skipped
        assert out.env_fingerprint is None or out.env_fingerprint == ""

        # NEUTRAL_IR
        t = _IcountTarget([(_r(10000), _r(10000))], probe_covers=["src/"])
        cand = _C(id="ir-neu", hypothesis="noop-ish",
                  patch=_P([_Ed("src/opt.rs", "a", "b")]))
        with redirect_stderr(io.StringIO()):
            out = _evalmod.evaluate(t, "base", _P([]), cand, 2, floors, objs)
        assert out.verdict == _V.NEUTRAL_IR, out.verdict

        # REGRESSED_IR
        t = _IcountTarget([(_r(10000), _r(12000))], probe_covers=["src/"])
        cand = _C(id="ir-reg", hypothesis="oops",
                  patch=_P([_Ed("src/opt.rs", "a", "b")]))
        with redirect_stderr(io.StringIO()):
            out = _evalmod.evaluate(t, "base", _P([]), cand, 2, floors, objs)
        assert out.verdict == _V.REGRESSED_IR, out.verdict

        # NO_COVERAGE on disjoint files
        t = _IcountTarget([(_r(10000), _r(9000))],
                          probe_covers=["crates/mega-evm/src/evm"])
        cand = _C(id="ir-nc", hypothesis="unrelated file",
                  patch=_P([_Ed("crates/other/src/x.rs", "a", "b")]))
        with redirect_stderr(io.StringIO()):
            out = _evalmod.evaluate(t, "base", _P([]), cand, 2, floors, objs)
        assert out.verdict == _V.NO_COVERAGE, out.verdict

        # absent probe_covers → warn + proceed (still measures Ir)
        t = _IcountTarget([(_r(10000), _r(9000))], probe_covers=[])
        cand = _C(id="ir-warn", hypothesis="no covers field",
                  patch=_P([_Ed("anywhere/x.rs", "a", "b")]))
        buf = io.StringIO()
        with redirect_stderr(buf):
            out = _evalmod.evaluate(t, "base", _P([]), cand, 2, floors, objs)
        assert out.verdict == _V.ACCEPTED_IR, out.verdict
        assert "probe_covers" in buf.getvalue()

        # locality passthrough with cache evidence reaches Gate 2 (wall-clock)
        base_r = _r(10000, d1mr=100, dlmr=50)
        cand_r = _r(10005, d1mr=80, dlmr=40)  # within ε, cache improves
        t = _IcountTarget([(base_r, cand_r)], probe_covers=["src/"])
        cand = _C(id="ir-loc", hypothesis="better locality",
                  patch=_P([_Ed("src/opt.rs", "a", "b")]), category="locality")
        with redirect_stderr(io.StringIO()):
            out = _evalmod.evaluate(t, "base", _P([]), cand, 3, floors, objs,
                                    aa_runs=1, bench_scales=(1,))
        # MockTarget makes FAST edits ~5% faster → ACCEPTED via wall-clock Gate 2
        assert out.verdict == _V.ACCEPTED, (out.verdict, out.notes)
        assert out.ir_delta_pct is not None  # rode through from Gate 1.5
        assert any("passthrough" in n or "locality" in n.lower() for n in out.notes)

        # locality WITHOUT cache evidence → NEUTRAL_IR (never reaches Gate 2)
        t = _IcountTarget([(_r(10000, d1mr=100, dlmr=50),
                            _r(10005, d1mr=100, dlmr=50))],
                          probe_covers=["src/"])
        cand = _C(id="ir-loc2", hypothesis="locality claim, no cache win",
                  patch=_P([_Ed("src/opt.rs", "a", "b")]), category="locality")
        with redirect_stderr(io.StringIO()):
            out = _evalmod.evaluate(t, "base", _P([]), cand, 3, floors, objs)
        assert out.verdict == _V.NEUTRAL_IR, out.verdict
    finally:
        _subprocess.run = _real_sp_run
        del os.environ["ARO_SKIP_SELFCHECK"]
    print("#40d OK: evaluate() Ir gate (all verdicts + locality + coverage warn)")

    # --- record extension: icount verdicts carry new fields; non-icount byte-identical ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # lessons: with Ir fields
        os.environ["ARO_PERMTREE_DIR"] = str(d / "pt")
        import importlib
        importlib.reload(_pt)
        # Point lessons at a temp file via monkeypatch of _PATH
        orig_path = _les._PATH
        _les._PATH = d / "lessons.jsonl"
        try:
            _les.append("t", "cpu win", "accepted-ir", delta_pct=-2.5,
                        note="Ir gate", ir_delta_pct=-2.5,
                        profile_fingerprint="rustc 1.80|deadbeef")
            row = json.loads(_les._PATH.read_text().splitlines()[0])
            assert row["ir_delta_pct"] == -2.5
            assert row["profile_fingerprint"] == "rustc 1.80|deadbeef"
            # non-icount path: no extra keys
            _les.append("t", "old path", "within-noise", delta_pct=0.1, note="aa")
            row2 = json.loads(_les._PATH.read_text().splitlines()[1])
            assert "ir_delta_pct" not in row2
            assert "profile_fingerprint" not in row2
            # keys match the legacy shape
            assert set(row2.keys()) == {"ts", "target", "change", "verdict",
                                        "delta_pct", "note"}

            rec = _pt.record("spec-ic", workload="w", fn="f", base_state="origin",
                             verdict="accepted-ir", regime="strict", delta=-2.5,
                             ir_delta_pct=-2.5,
                             profile_fingerprint="rustc 1.80|deadbeef")
            assert rec["ir_delta_pct"] == -2.5
            assert rec["profile_fingerprint"] == "rustc 1.80|deadbeef"
            rec2 = _pt.record("spec-ic", workload="w", fn="g", base_state="origin",
                              verdict="within-noise", regime="strict", delta=0.0)
            assert "ir_delta_pct" not in rec2
            assert "profile_fingerprint" not in rec2
        finally:
            _les._PATH = orig_path
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#40e OK: lessons/permtree additive Ir fields; non-icount shape preserved")

    # --- env ARO_ICOUNT_EPSILON wins over default ---
    try:
        os.environ["ARO_ICOUNT_EPSILON"] = "1.0"
        assert _ic.ir_epsilon_pct(None) == 1.0
        # with 1% ε, a −0.5% Δ is neutral
        d = _ic.judge_ir(_r(10000), _r(9950), epsilon_pct=_ic.ir_epsilon_pct(None),
                         locality=False)
        assert d.verdict == _V.NEUTRAL_IR
    finally:
        del os.environ["ARO_ICOUNT_EPSILON"]
    print("#40 OK: instruction-count gate (parser/profile/gate/records)")


def case_30():
    """T4: refuted-by-icount vocabulary + recheck-debts scaffold (mocked Ir)."""
    import importlib
    import os
    from types import SimpleNamespace
    from aro import permtree as _pt
    from aro import recheck_debts as _rd
    from aro import lessons as _les
    from aro.types import EvalOutcome, MetricDelta, Verdict as _V
    from aro.attempt import _VERDICT_RANK
    from aro.types import is_accept_verdict

    # --- vocabulary: CLOSED, not open, not accept ---
    assert "refuted-by-icount" in _pt._CLOSED_VERDICTS
    assert "refuted-by-icount" not in _pt._OPEN_VERDICTS
    assert "refuted-by-icount" not in _pt._ACCEPT_VERDICTS
    assert not is_accept_verdict(_V.REFUTED_BY_ICOUNT)
    assert _VERDICT_RANK.get("refuted-by-icount", -1) == 0
    print("#41a OK: refuted-by-icount is CLOSED / not-accept")

    # --- last-record-wins: accepted → refuted closes the debt / node ---
    with tempfile.TemporaryDirectory() as d:
        os.environ["ARO_PERMTREE_DIR"] = d
        importlib.reload(_pt)
        try:
            _pt.record("spec-r", workload="spec-r", fn="add", base_state="origin",
                       verdict="accepted", regime="relaxed", delta=-2.14, pct=3.5)
            _pt.record("spec-r", workload="spec-r", fn="hot", base_state="origin",
                       verdict="noise-limited", regime="strict", delta=-1.0, pct=5.0)
            debts = _pt.open_debts(_pt.load("spec-r"))
            assert {x["fn"] for x in debts} == {"hot"}
            # refute the accepted node — must NOT reopen it
            _pt.record("spec-r", workload="spec-r", fn="add", base_state="origin",
                       verdict="refuted-by-icount", regime="relaxed", delta=-2.14,
                       pct=3.5, hypothesis="CodSpeed 306/306 untouched (#332)")
            ns = _pt.nodes("spec-r")
            assert ns[_pt.node_key("spec-r", "add", "origin")]["verdict"] == "refuted-by-icount"
            assert ns[_pt.node_key("spec-r", "add", "origin")]["visits"] == 2
            u = _pt.union(["spec-r"])
            assert all(r["fn"] != "add" or r["verdict"] != "accepted"
                       for r in u["accepted"])
            assert "add" not in {c["fn"] for c in u["open_cases"]}
            debts2 = _pt.open_debts(_pt.load("spec-r"))
            assert {x["fn"] for x in debts2} == {"hot"}
        finally:
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#41b OK: refuted-by-icount last-record-wins closes node")

    # --- recheck-debts: patch recovery + mocked evaluate → ledger write ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        os.environ["ARO_PERMTREE_DIR"] = str(d / "pt")
        importlib.reload(_pt)
        # also redirect lessons
        orig_les = _les._PATH
        _les._PATH = d / "lessons.jsonl"
        try:
            runs = d / "runs"
            att = runs / "camp" / "a1"
            (att / "patches").mkdir(parents=True)
            # store a recoverable patch
            patch_txt = (
                "--- edit 1 ---\n"
                "path: src/hot.rs\n"
                "<<<<<<< SEARCH\n"
                "a + b\n"
                "=======\n"
                "a.wrapping_add(b)\n"
                ">>>>>>> REPLACE\n"
            )
            (att / "patches" / "agent-r0.txt").write_text(patch_txt)
            (att / "records.jsonl").write_text(json.dumps({
                "id": "agent-r0",
                "verdict": "noise-limited",
                "hypothesis": "strength-reduce add on hot path",
                "metrics": [], "notes": [],
            }) + "\n")

            # seed an open debt pointing at that attempt
            _pt.record("mock-debt", workload="mock-debt", fn="hot_add",
                       base_state="origin", verdict="noise-limited",
                       regime="strict", delta=-1.5, pct=4.0,
                       files=["src/hot.rs"],
                       hypothesis="strength-reduce add on hot path",
                       events_ref=str(att))  # absolute path, no # form

            # also a debt with no recoverable patch
            _pt.record("mock-debt", workload="mock-debt", fn="ghost",
                       base_state="origin", verdict="no-attempt",
                       regime="strict", pct=2.0, hypothesis="never tried",
                       events_ref=str(runs / "missing" / "a9"))

            # recoverability helpers
            debt_hot = next(x for x in _pt.open_debts(_pt.load("mock-debt"))
                            if x["fn"] == "hot_add")
            cand, pf, note = _rd.recover_candidate(debt_hot)
            assert cand is not None and pf is not None, (cand, pf, note)
            assert len(cand.patch.edits) == 1

            debt_ghost = next(x for x in _pt.open_debts(_pt.load("mock-debt"))
                              if x["fn"] == "ghost")
            c2, p2, n2 = _rd.recover_candidate(debt_ghost)
            assert c2 is None and (
                "regenerate" in n2.lower() or "no recoverable" in n2.lower()), n2

            # mocked evaluate: Ir neutral → refuted-by-icount write-back
            def _eval_neutral(cand):
                return EvalOutcome(
                    candidate_id=cand.id, verdict=_V.NEUTRAL_IR,
                    deltas=[MetricDelta("Ir", 10000, 10000, 0.0, -0.05, 0.05,
                                        0.1, False, False)],
                    notes=["verdict: neutral-ir — |ΔIr| ≤ ε"],
                    ir_delta_pct=0.0, profile_fingerprint="rustc|testfp")

            spec = SimpleNamespace(name="mock-debt", metric_names=["ns_per_call"])
            results = _rd.recheck_debts(spec, evaluate_fn=_eval_neutral, write=True)
            by_fn = {r["fn"]: r for r in results}
            assert by_fn["hot_add"]["status"] == "rechecked", by_fn["hot_add"]
            assert by_fn["hot_add"]["verdict"] == "refuted-by-icount"
            assert by_fn["hot_add"]["ir_delta_pct"] == 0.0
            assert by_fn["ghost"]["status"] == "regenerate"

            # ledger last-record-wins closed hot_add; ghost still open
            debts_after = _pt.open_debts(_pt.load("mock-debt"))
            assert {x["fn"] for x in debts_after} == {"ghost"}, debts_after
            latest = _pt.nodes("mock-debt")[_pt.node_key("mock-debt", "hot_add", "origin")]
            assert latest["verdict"] == "refuted-by-icount"
            assert latest.get("profile_fingerprint") == "rustc|testfp"
            assert latest.get("ir_delta_pct") == 0.0

            # lessons got the refutation
            les_rows = [json.loads(ln) for ln in _les._PATH.read_text().splitlines() if ln.strip()]
            assert any(r["verdict"] == "refuted-by-icount" for r in les_rows)

            # true Ir win maps to accepted-ir (re-open a debt, recheck with win)
            _pt.record("mock-debt", workload="mock-debt", fn="win_fn",
                       base_state="origin", verdict="noise-limited",
                       regime="strict", delta=-2.0, pct=3.0,
                       files=["src/hot.rs"],
                       hypothesis="real win buried by floor",
                       events_ref=str(att))

            def _eval_win(cand):
                return EvalOutcome(
                    candidate_id=cand.id, verdict=_V.ACCEPTED_IR,
                    deltas=[MetricDelta("Ir", 10000, 9000, -10.0, -10.1, -9.9,
                                        0.1, True, False)],
                    notes=["verdict: accepted-ir"],
                    ir_delta_pct=-10.0, profile_fingerprint="rustc|win")

            # only recheck win_fn: inject evaluate that always wins; open set
            # still has ghost + win_fn. recover_candidate for ghost fails.
            results2 = _rd.recheck_debts(spec, evaluate_fn=_eval_win, write=True)
            win = next(r for r in results2 if r["fn"] == "win_fn")
            assert win["status"] == "rechecked" and win["verdict"] == "accepted-ir"
            assert win["ir_delta_pct"] == -10.0
            # accepted-ir is closed (not open debt)
            assert "win_fn" not in {x["fn"] for x in _pt.open_debts(_pt.load("mock-debt"))}
            assert any(r.get("verdict") == "accepted-ir"
                       for r in _pt.union(["mock-debt"])["accepted"])

            # map_outcome_verdict unit checks
            assert _rd.map_outcome_verdict("neutral-ir") == "refuted-by-icount"
            assert _rd.map_outcome_verdict("regressed-ir") == "refuted-by-icount"
            assert _rd.map_outcome_verdict("accepted-ir") == "accepted-ir"
            assert _rd.map_outcome_verdict("no-coverage") == "no-coverage"
            assert _rd.map_outcome_verdict("build-failed") == "build-failed"
        finally:
            _les._PATH = orig_les
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#41c OK: recheck-debts recover + mocked Ir → refuted/accepted write-back")

    # --- #41d: CLI --list-only must not touch SpecTarget / missing target path ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        os.environ["ARO_PERMTREE_DIR"] = str(d / "pt")
        importlib.reload(_pt)
        try:
            missing_repo = d / "no-such-target-repo"
            assert not missing_repo.exists()
            _pt.record("list-only-smoke", workload="list-only-smoke", fn="hot",
                       base_state="origin", verdict="noise-limited",
                       regime="strict", delta=-1.0, pct=4.0,
                       hypothesis="list-only must not need a target checkout")
            spec_path = d / "list-only-smoke.json"
            spec_path.write_text(json.dumps({
                "name": "list-only-smoke",
                "target_repo": {"path": str(missing_repo)},
                "hot_path": {"file": "src/lib.rs", "fn": "hot"},
                "metric": "ns_per_call",
                "benchmark_probe": {
                    "pkg": "p", "example": "e",
                    "probe": "fixtures/mini-target/probes/mini_target.rs",
                },
                "correctness_oracle": {"build": ["true"], "test": ["true"]},
                "constraints": {"editable": ["src"]},
            }))
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                _rd.cli(SimpleNamespace(
                    spec=str(spec_path), dry_run=False, list_only=True,
                    runs_root=None))
            out = buf.getvalue()
            assert "open debts for list-only-smoke:" in out, out
            assert "hot" in out and "noise-limited" in out, out
            assert not missing_repo.exists()
        finally:
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#41d OK: recheck-debts --list-only exits cleanly when target path absent")
    print("#41 OK: refuted-by-icount vocabulary + recheck-debts scaffold")


def case_31():
    """T3: terminal criterion-Ir gate + manifest mergeable + CLI smoke."""
    import importlib
    import os
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import manifest as _mf
    from aro import permtree as _pt
    from aro import lessons as _les
    from aro.attempt import _VERDICT_RANK

    # --- vocabulary: TERMINAL_* are CLOSED, not open, not accept ---
    for v in _tm.ALL_TERMINAL_VERDICTS:
        assert v in _pt._CLOSED_VERDICTS, v
        assert v not in _pt._OPEN_VERDICTS, v
        assert v not in _pt._ACCEPT_VERDICTS, v
        assert v in _VERDICT_RANK, v
    assert _VERDICT_RANK["TERMINAL_CONFIRMED"] > _VERDICT_RANK["TERMINAL_UNTOUCHED"]
    print("#42a OK: TERMINAL_* vocabulary closed / ranked")

    # --- parse measure JSON ---
    doc = _tm.parse_measure_stdout(json.dumps({
        "rows": {
            "mega_bench/sload": {"instr_count": 10000, "ns": 12.3},
            "mega_bench/sstore": {"instr_count": 20000},
        },
        "meta": {"rustc": "rustc 1.80.0", "profile_fingerprint": "fp-abc"},
    }))
    assert doc.rows == {"mega_bench/sload": 10000, "mega_bench/sstore": 20000}
    assert doc.profile_fingerprint == "fp-abc"
    try:
        _tm.parse_measure_stdout("")
        assert False, "empty stdout should hard-error"
    except _tm.TerminalError:
        pass
    try:
        _tm.parse_measure_stdout(json.dumps({
            "rows": {"x": {"ns": 1}}, "meta": {}}))
        assert False, "missing instr_count should hard-error"
    except _tm.TerminalError as e:
        assert "instr_count" in str(e)
    # absent / empty profile_fingerprint must hard-error (never default to '')
    for bad_meta in (
        {},
        {"rustc": "r"},
        {"profile_fingerprint": ""},
        {"profile_fingerprint": "   "},
    ):
        try:
            _tm.parse_measure_stdout(json.dumps({
                "rows": {"x": {"instr_count": 1}}, "meta": bad_meta}))
            assert False, f"empty/missing fingerprint should hard-error: {bad_meta!r}"
        except _tm.TerminalError as e:
            assert "profile_fingerprint" in str(e), e
    try:
        _tm.parse_measure_stdout(json.dumps({
            "rows": {"x": {"instr_count": 1}}}))  # no meta key at all
        assert False, "missing meta should hard-error on fingerprint"
    except _tm.TerminalError as e:
        assert "profile_fingerprint" in str(e)
    print("#42b OK: measure JSON parse + hard errors")

    def _doc(rows, fp="fp-abc"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    # CONFIRMED: one improved, none regressed
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 20000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_CONFIRMED, r
    assert r.bench_ir_rows["a"] == -10.0
    assert "b" not in r.bench_ir_rows  # exact zero Δ omitted
    assert r.profile_fingerprint == "fp-abc"

    # UNTOUCHED: all within ε (exact equal)
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 10000, "b": 20000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_UNTOUCHED, r
    assert r.bench_ir_rows == {}

    # UNTOUCHED: tiny noise inside ε
    r = _tm.judge_terminal(
        _doc({"a": 10000}),
        _doc({"a": 10005}),  # +0.05%
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_UNTOUCHED, r

    # REGRESSED
    r = _tm.judge_terminal(
        _doc({"a": 10000}),
        _doc({"a": 11000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_REGRESSED, r
    assert r.bench_ir_rows["a"] == 10.0

    # MIXED
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 22000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_MIXED, r
    assert r.bench_ir_rows["a"] == -10.0 and r.bench_ir_rows["b"] == 10.0

    # fingerprint mismatch → hard error (never a verdict)
    try:
        _tm.judge_terminal(
            _doc({"a": 10000}, fp="fp-1"),
            _doc({"a": 9000}, fp="fp-2"),
            epsilon_pct=0.1)
        assert False, "fingerprint mismatch must hard-error"
    except _tm.TerminalError as e:
        assert "config drift" in str(e) and "fp-1" in str(e)

    # row-set mismatch → hard error
    try:
        _tm.judge_terminal(
            _doc({"a": 10000, "b": 1}),
            _doc({"a": 9000, "c": 1}),
            epsilon_pct=0.1)
        assert False, "row-set mismatch must hard-error"
    except _tm.TerminalError as e:
        assert "row-set mismatch" in str(e)
        assert "dropped" in str(e) and "new" in str(e)
    print("#42c OK: terminal verdicts CONFIRMED/UNTOUCHED/REGRESSED/MIXED + hard errors")

    # --- measure via injectable runner (no real binary) ---
    calls = []

    seen_timeouts = []

    def _runner(cmd, timeout=None):
        calls.append(list(cmd))
        seen_timeouts.append(timeout)
        # Return different docs based on checkout path suffix
        checkout = cmd[cmd.index("--checkout") + 1]
        if "base" in checkout:
            body = {"rows": {"row": {"instr_count": 10000}},
                    "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
        else:
            body = {"rows": {"row": {"instr_count": 9000}},
                    "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
        return json.dumps(body), "", 0

    sp = SimpleNamespace(
        name="term-demo",
        terminal_bench_targets=["mega_bench"],
        terminal_bench_filter=None,
        measure_bin="/fake/mega-bench-reporter",
        icount_epsilon_pct=0.1,
        timeout=1800,
        bench={"pkg": "mega-evm"},
        raw={},
    )
    # rounds=1 keeps this smoke path to a single measure per side; median-of-N
    # is covered in case_32. Empty floors map → default floor (1.0%) still
    # classifies the -10% Δ as CONFIRMED.
    result = _tm.run_terminal(
        sp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
        rounds=1, floors={}, skip_selfcheck=True)
    assert result.verdict == _tm.TERMINAL_CONFIRMED
    assert result.rounds == 1
    assert result.floors_source == "default"
    assert len(calls) == 2
    assert "--instructions" in calls[0] and "mega_bench" in calls[0]
    assert calls[0][0] == "/fake/mega-bench-reporter"
    # default timeout = 4 × spec.timeout, threaded through the runner seam
    assert seen_timeouts == [7200.0, 7200.0], seen_timeouts
    assert _tm.resolve_terminal_timeout(sp) == 7200.0
    # target JSON field terminal_timeout_secs overrides
    sp_to = SimpleNamespace(timeout=1800, terminal_timeout_secs=99, raw={})
    assert _tm.resolve_terminal_timeout(sp_to) == 99.0
    sp_to_raw = SimpleNamespace(timeout=1800, raw={"terminal_timeout_secs": 42})
    assert _tm.resolve_terminal_timeout(sp_to_raw) == 42.0
    # TimeoutExpired from runner → TerminalError (same pattern as valgrind timeout)
    import subprocess as _subprocess

    def _slow(cmd, timeout=None):
        raise _subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    try:
        _tm.measure_checkout(
            "/tmp/base-wt", package="p", bench_targets=["t"],
            measure_bin="/fake/r", timeout=12, runner=_slow)
        assert False, "TimeoutExpired must become TerminalError"
    except _tm.TerminalError as e:
        assert "timed out" in str(e) and "12" in str(e)

    # ARO_MEASURE_BIN wins
    try:
        os.environ["ARO_MEASURE_BIN"] = "/env/reporter"
        assert _tm.resolve_measure_bin(sp) == "/env/reporter"
    finally:
        del os.environ["ARO_MEASURE_BIN"]

    # missing measure_bin → clear TerminalError (not traceback)
    bare = SimpleNamespace(measure_bin=None, raw={}, name="x",
                           terminal_bench_targets=["t"], bench={"pkg": "p"},
                           icount_epsilon_pct=0.1, terminal_bench_filter=None)
    try:
        _tm.resolve_measure_bin(bare)
        assert False
    except _tm.TerminalError as e:
        assert "ARO_MEASURE_BIN" in str(e) or "measure_bin" in str(e)
    print("#42d OK: injectable runner + measure_bin resolution")

    # --- manifest: terminal_required tightens mergeable; no-config byte-identical ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def J(o):
            return json.dumps(o)

        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")

        # (1) no terminal config → legacy shape + mergeable true (byte-identical)
        m0 = _mf.build_manifest(d)
        assert m0["accepted"][0]["mergeable"] is True
        assert "terminal" not in m0["accepted"][0]
        assert "bench_ir_rows" not in m0["accepted"][0]
        assert "terminal" not in m0

        # (2) terminal_required but no result yet → mergeable false
        m1 = _mf.build_manifest(d, terminal_required=True)
        assert m1["accepted"][0]["mergeable"] is False
        assert m1["accepted"][0]["terminal"] is None
        assert m1["accepted"][0]["bench_ir_rows"] == {}

        # (3) stamp CONFIRMED → mergeable true
        conf = _tm.TerminalResult(
            verdict=_tm.TERMINAL_CONFIRMED,
            bench_ir_rows={"mega_bench/sload": -3.2},
            profile_fingerprint="fp-abc",
            notes=["ok"],
        )
        m2 = _mf.build_manifest(d, terminal_result=conf, terminal_required=True)
        assert m2["accepted"][0]["mergeable"] is True
        assert m2["accepted"][0]["terminal"] == "TERMINAL_CONFIRMED"
        assert m2["accepted"][0]["bench_ir_rows"] == {"mega_bench/sload": -3.2}
        assert m2["accepted"][0]["profile_fingerprint"] == "fp-abc"
        assert m2["terminal"]["verdict"] == "TERMINAL_CONFIRMED"

        # (4) UNTOUCHED → mergeable false even with byte-identical/pass
        unt = _tm.TerminalResult(
            verdict=_tm.TERMINAL_UNTOUCHED,
            bench_ir_rows={},
            profile_fingerprint="fp-abc",
        )
        m3 = _mf.apply_terminal(dict(m0), unt, terminal_required=True)
        # apply_terminal mutates accepted entries; rebuild for clean apply
        m3 = _mf.build_manifest(d)
        m3 = _mf.apply_terminal(m3, unt, terminal_required=True)
        assert m3["accepted"][0]["mergeable"] is False
        assert m3["accepted"][0]["terminal"] == "TERMINAL_UNTOUCHED"

        # (5) relaxed/pass-risk stays non-mergeable even under CONFIRMED
        # Build a second event log shape via apply on a crafted entry:
        assert _mf.is_mergeable("relaxed", "pass",
                                terminal="TERMINAL_CONFIRMED",
                                terminal_required=True) is False
        assert _mf.is_mergeable("byte-identical", "pass-risk",
                                terminal="TERMINAL_CONFIRMED",
                                terminal_required=True) is False
        assert _mf.is_mergeable("byte-identical", "pass",
                                terminal="TERMINAL_CONFIRMED",
                                terminal_required=True) is True
        # no terminal config: CONFIRMED stamp is ignored
        assert _mf.is_mergeable("byte-identical", "pass",
                                terminal="TERMINAL_UNTOUCHED",
                                terminal_required=False) is True
    print("#42e OK: manifest mergeable terminal gate + backward-compatible shape")

    # --- record_terminal → lessons + permtree carry fingerprint ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        os.environ["ARO_PERMTREE_DIR"] = str(d / "pt")
        importlib.reload(_pt)
        orig = _les._PATH
        _les._PATH = d / "lessons.jsonl"
        try:
            res = _tm.TerminalResult(
                verdict=_tm.TERMINAL_UNTOUCHED,
                bench_ir_rows={},
                profile_fingerprint="rustc 1.80|deadbeef",
                notes=["verdict: TERMINAL_UNTOUCHED — probe-vs-bench divergence"],
            )
            _tm.record_terminal("mega-evm-v2", res, fn="sload",
                                hypothesis="hoist sload oracle")
            les = json.loads(_les._PATH.read_text().splitlines()[0])
            assert les["verdict"] == "TERMINAL_UNTOUCHED"
            assert les["profile_fingerprint"] == "rustc 1.80|deadbeef"
            ns = _pt.nodes("mega-evm-v2")
            key = _pt.node_key("mega-evm-v2", "sload", "origin")
            assert ns[key]["verdict"] == "TERMINAL_UNTOUCHED"
            assert ns[key]["profile_fingerprint"] == "rustc 1.80|deadbeef"
        finally:
            _les._PATH = orig
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#42f OK: terminal lessons/permtree record with fingerprint")

    # --- CLI smoke: --list without measure binary; missing bin clear error ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # fixture spec WITHOUT measure_bin, WITH terminal targets; missing repo path
        missing_repo = d / "no-repo"
        spec_path = d / "term-smoke.json"
        spec_path.write_text(json.dumps({
            "name": "term-smoke",
            "target_repo": {"path": str(missing_repo)},
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "metric": "ns_per_call",
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "terminal_bench_targets": ["mega_bench"],
            "icount_epsilon_pct": 0.1,
        }))
        import io
        from contextlib import redirect_stdout, redirect_stderr
        buf = io.StringIO()
        with redirect_stdout(buf):
            _tm.cli(SimpleNamespace(
                spec=str(spec_path), list=True, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=None, hypothesis=None, events_ref=None))
        out = buf.getvalue()
        assert "terminal config for term-smoke" in out, out
        assert "mega_bench" in out
        assert "UNSET" in out  # measure_bin unset is reported, not a crash
        assert "gate active:            True" in out

        # measure path without bin → exit 2 with clear message.
        # Skip selfcheck so this smoke still exercises the measure_bin error
        # (selfcheck hard-error is covered in case_33).
        err = io.StringIO()
        os.environ["ARO_SKIP_SELFCHECK"] = "1"
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                _tm.cli(SimpleNamespace(
                    spec=str(spec_path), list=False, dry_run=False,
                    baseline=str(d / "b"), candidate=str(d / "c"),
                    out=None, record=False, fn=None, update_manifest=None,
                    hypothesis=None, events_ref=None))
            assert False, "missing measure_bin must SystemExit"
        except SystemExit as se:
            assert se.code == 2
        finally:
            del os.environ["ARO_SKIP_SELFCHECK"]
        assert "measure binary unset" in err.getvalue() or \
               "ARO_MEASURE_BIN" in err.getvalue()

        # mocked runner end-to-end via run_terminal already covered; CLI with
        # env measure_bin + custom runner is module-level (cli uses subprocess).
        # Exercise build_measure_cmd shape:
        cmd = _tm.build_measure_cmd(
            "/bin/reporter", "/wt", package="mega-evm",
            bench_targets=["mega_bench", "other"], bench_filter="sload")
        assert cmd[:2] == ["/bin/reporter", "measure"]
        assert "--instructions" in cmd
        assert cmd.count("--bench-target") == 2
        assert "--bench-filter" in cmd and "sload" in cmd
    print("#42g OK: CLI --list / missing-bin message / measure cmd shape")
    print("#42 OK: terminal criterion-Ir gate (T3)")


def case_32():
    """T8: per-row terminal floors + median-of-N sampling (hermetic)."""
    import io
    import os
    from contextlib import redirect_stderr, redirect_stdout
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import cli as _cli

    def _doc(rows, fp="fp-abc"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    # --- calibration math: max pairwise |Δ%| × 2, clamped to ε ---------------
    # values 10000, 10020 → pairwise = 20/10000*100 = 0.2% → ×2 = 0.4%
    assert abs(_tm.pairwise_abs_pct(10000, 10020) - 0.2) < 1e-9
    assert abs(_tm.max_pairwise_delta_pct([10000, 10020, 9990]) -
               _tm.pairwise_abs_pct(10020, 9990)) < 1e-9
    fl = _tm.calibrate_row_floor([10000, 10020], min_floor_pct=0.1)
    assert abs(fl - 0.4) < 1e-9, fl
    # tiny noise → clamp to ε
    fl_lo = _tm.calibrate_row_floor([10000, 10001], min_floor_pct=0.1)
    assert abs(fl_lo - 0.1) < 1e-9, fl_lo  # 0.01% × 2 = 0.02 → clamp 0.1
    # single value → min floor
    assert _tm.calibrate_row_floor([10000], min_floor_pct=0.1) == 0.1

    d1 = _doc({"a": 10000, "b": 20000})
    d2 = _doc({"a": 10040, "b": 20000})  # a: 0.4% pair → floor 0.8%; b: 0
    d3 = _doc({"a": 9990, "b": 20100})
    floors = _tm.compute_floors_from_docs([d1, d2, d3], min_floor_pct=0.1)
    assert floors["a"] == _tm.calibrate_row_floor(
        [10000, 10040, 9990], min_floor_pct=0.1)
    assert floors["b"] == _tm.calibrate_row_floor(
        [20000, 20000, 20100], min_floor_pct=0.1)
    assert floors["b"] >= 0.1
    print("#43a OK: calibration math (max pairwise ×2, ε clamp)")

    # --- floors file write / load + meta + staleness warnings ----------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_FLOORS_DIR"] = str(td)
        try:
            meta = {
                "calibrated_at": "2020-01-01T00:00:00Z",  # stale
                "rounds": 3,
                "checkout_describe": "deadbeef",
                "measure_bin": "/fake/reporter",
                "rustc": "rustc 0.0.0-stale",
            }
            dest = _tm.write_floors("demo", {"row/x": 0.5, "row/y": 0.1}, meta=meta)
            assert dest == td / "demo.json"
            loaded, meta_l, warns = _tm.load_floors("demo")
            assert loaded == {"row/x": 0.5, "row/y": 0.1}
            assert meta_l["rounds"] == 3
            assert meta_l["checkout_describe"] == "deadbeef"
            # age + rustc mismatch (when rustc is present) → warnings, not errors
            assert any("old" in w or "stale" in w.lower() or "calibrated_at" in w
                       for w in warns), warns
            # missing file → empty + warning
            empty, _, w2 = _tm.load_floors("no-such-spec")
            assert empty == {}
            assert any("no calibrated file" in w for w in w2)

            # Non-finite / non-positive floors must be skipped (fallback to
            # default_floor_pct) with a warning naming the row + bad value.
            # Accepting NaN would make both comparisons False → silent UNTOUCHED
            # on a real regression; negative floors invert classification.
            bad_payload = {
                "meta": {
                    "calibrated_at": "2026-01-01T00:00:00Z",
                    "rounds": 3,
                    "rustc": "rustc 1.80.0",
                },
                "floors": {
                    "ok": 0.5,
                    "nan_row": float("nan"),
                    "inf_row": float("inf"),
                    "neg_row": -1.0,
                    "zero_row": 0.0,
                },
            }
            (td / "bad.json").write_text(json.dumps(bad_payload) + "\n")
            loaded_bad, _, warns_bad = _tm.load_floors("bad")
            assert loaded_bad == {"ok": 0.5}, loaded_bad
            for bad_key in ("nan_row", "inf_row", "neg_row", "zero_row"):
                assert bad_key not in loaded_bad
                assert any(bad_key in w for w in warns_bad), (bad_key, warns_bad)
            # Fallback classification: +5% with default floor 1.0% → REGRESSED.
            # (NaN floor would have classified the same Δ as UNTOUCHED.)
            r_fb = _tm.judge_terminal(
                _doc({"nan_row": 10000}), _doc({"nan_row": 10500}),
                epsilon_pct=0.1, floors=loaded_bad, default_floor_pct=1.0,
                floors_source="default")
            assert r_fb.verdict == _tm.TERMINAL_REGRESSED, r_fb
            assert r_fb.rows[0].floor_pct == 1.0
            assert r_fb.rows[0].status == "regressed"
        finally:
            del os.environ["ARO_FLOORS_DIR"]
    print("#43b OK: floors file write/load + staleness warnings")

    # --- floor-aware classification ------------------------------------------
    # floors all = ε → backward-comparable with legacy ε threshold
    r = _tm.judge_terminal(
        _doc({"a": 10000}), _doc({"a": 10005}),  # +0.05%
        epsilon_pct=0.1, floors={"a": 0.1}, floors_source="calibrated")
    assert r.verdict == _tm.TERMINAL_UNTOUCHED
    assert r.floors_source == "calibrated"
    assert r.rows[0].floor_pct == 0.1

    # Δ = +0.5% with floor 1.0 → untouched; with floor 0.1 → regressed
    r = _tm.judge_terminal(
        _doc({"a": 10000}), _doc({"a": 10050}),
        epsilon_pct=0.1, floors={}, default_floor_pct=1.0, floors_source="default")
    assert r.verdict == _tm.TERMINAL_UNTOUCHED, r
    r = _tm.judge_terminal(
        _doc({"a": 10000}), _doc({"a": 10050}),
        epsilon_pct=0.1, floors={"a": 0.1}, floors_source="calibrated")
    assert r.verdict == _tm.TERMINAL_REGRESSED, r

    # mixed floors: calibrated a (0.1) + default b (1.0)
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9900, "b": 20100}),  # a -1%, b +0.5%
        epsilon_pct=0.1,
        floors={"a": 0.1},
        default_floor_pct=1.0,
        floors_source="mixed",
    )
    assert r.verdict == _tm.TERMINAL_CONFIRMED, r  # a improved, b within default
    assert r.floors_source == "mixed"
    assert r.rows[0].status == "improved" and r.rows[1].status == "untouched"

    # calibrated tiny floor still yields UNTOUCHED for equal rows
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 10000, "b": 20000}),
        epsilon_pct=0.1, floors={"a": 0.1, "b": 0.1}, floors_source="calibrated")
    assert r.verdict == _tm.TERMINAL_UNTOUCHED
    # payload carries rounds + floors_source
    d = r.to_dict()
    assert d["floors_source"] == "calibrated"
    assert "rounds" in d and "floor_pct" in d["rows"][0]
    print("#43c OK: floor-aware classification (calibrated/default/mixed)")

    # --- median-of-N (odd + even) --------------------------------------------
    # odd: median is middle element
    assert _tm.median_ir([9000, 10000, 11000]) == 10000
    # even: average of two middle, rounded
    assert _tm.median_ir([9000, 10000, 11000, 12000]) == 10500

    med = _tm.median_measure_docs([
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 21000}),
        _doc({"a": 11000, "b": 19000}),
    ])
    assert med.rows["a"] == 10000
    assert med.rows["b"] == 20000
    assert med.profile_fingerprint == "fp-abc"

    # fingerprint drift across rounds → hard error
    try:
        _tm.median_measure_docs([
            _doc({"a": 1}, fp="fp-1"),
            _doc({"a": 1}, fp="fp-2"),
        ])
        assert False
    except _tm.TerminalError as e:
        assert "config drift" in str(e)

    # row present in some rounds but missing in one of the SAME side →
    # row-set-mismatch TerminalError before any median is computed
    # (symmetric to fingerprint-drift above)
    try:
        _tm.median_measure_docs([
            _doc({"a": 10000, "b": 20000}),
            _doc({"a": 10010}),            # b dropped mid-side
            _doc({"a": 9990, "b": 20100}),
        ])
        assert False, "row-set mismatch across rounds of same side must hard-error"
    except _tm.TerminalError as e:
        assert "row-set mismatch" in str(e), e
        assert "dropped" in str(e) and "b" in str(e)

    # run_terminal measures rounds times per side
    calls = []

    def _runner(cmd, timeout=None):
        calls.append(list(cmd))
        checkout = cmd[cmd.index("--checkout") + 1]
        # Vary Ir slightly across calls; median of three still yields a clear win.
        side_calls = [c for c in calls if c[c.index("--checkout") + 1] == checkout]
        i = len(side_calls)  # 1-based after append
        if "base" in checkout:
            # 10000, 10010, 9990 → median 10000
            ir = {1: 10000, 2: 10010, 3: 9990, 4: 10000}.get(i, 10000)
        else:
            # 9000, 9010, 8990 → median 9000 → Δ = -10%
            ir = {1: 9000, 2: 9010, 3: 8990, 4: 9000}.get(i, 9000)
        body = {"rows": {"row": {"instr_count": ir}},
                "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
        return json.dumps(body), "", 0

    sp = SimpleNamespace(
        name="term-med",
        terminal_bench_targets=["mega_bench"],
        terminal_bench_filter=None,
        measure_bin="/fake/reporter",
        icount_epsilon_pct=0.1,
        terminal_default_floor_pct=1.0,
        timeout=1800,
        bench={"pkg": "mega-evm"},
        raw={},
    )
    result = _tm.run_terminal(
        sp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
        rounds=3, floors={"row": 0.1}, skip_selfcheck=True)
    assert result.verdict == _tm.TERMINAL_CONFIRMED, result
    assert result.rounds == 3
    assert result.floors_source == "calibrated"
    assert len(calls) == 6  # 3 base + 3 cand
    assert result.bench_ir_rows["row"] == -10.0
    # even rounds via measure_checkout_rounds
    calls.clear()
    result4 = _tm.run_terminal(
        sp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
        rounds=4, floors={"row": 0.1}, skip_selfcheck=True)
    assert result4.rounds == 4
    assert len(calls) == 8
    assert result4.verdict == _tm.TERMINAL_CONFIRMED

    # env ARO_TERMINAL_ROUNDS wins
    try:
        os.environ["ARO_TERMINAL_ROUNDS"] = "2"
        assert _tm.resolve_terminal_rounds(sp) == 2
    finally:
        del os.environ["ARO_TERMINAL_ROUNDS"]
    assert _tm.resolve_terminal_rounds(sp) == 3  # default
    sp_r = SimpleNamespace(terminal_measure_rounds=5, raw={})
    assert _tm.resolve_terminal_rounds(sp_r) == 5
    print("#43d OK: median-of-N (odd/even) + rounds resolution")

    # --- calibrate via injectable runner + CLI dry-run -----------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_FLOORS_DIR"] = str(td)
        try:
            seq = [
                {"rows": {"r1": {"instr_count": 10000}, "r2": {"instr_count": 50000}},
                 "meta": {"profile_fingerprint": "fp", "rustc": "rustc 1.80"}},
                {"rows": {"r1": {"instr_count": 10020}, "r2": {"instr_count": 50000}},
                 "meta": {"profile_fingerprint": "fp", "rustc": "rustc 1.80"}},
                {"rows": {"r1": {"instr_count": 9990}, "r2": {"instr_count": 50100}},
                 "meta": {"profile_fingerprint": "fp", "rustc": "rustc 1.80"}},
            ]
            it = iter(seq)

            def _cal_runner(cmd, timeout=None):
                body = next(it)
                return json.dumps(body), "", 0

            spc = SimpleNamespace(
                name="cal-demo",
                terminal_bench_targets=["mega_bench"],
                terminal_bench_filter=None,
                measure_bin="/fake/reporter",
                icount_epsilon_pct=0.1,
                timeout=1800,
                bench={"pkg": "p"},
                raw={},
            )
            payload = _tm.run_calibrate(
                spc, "/tmp/checkout-wt", rounds=3, runner=_cal_runner,
                skip_selfcheck=True)
            assert Path(payload["path"]).is_file()
            assert payload["meta"]["rounds"] == 3
            assert "calibrated_at" in payload["meta"]
            assert "measure_bin" in payload["meta"]
            assert "r1" in payload["floors"] and "r2" in payload["floors"]
            assert payload["floors"]["r1"] == _tm.calibrate_row_floor(
                [10000, 10020, 9990], min_floor_pct=0.1)
            # re-load via load_floors
            fl, meta, _ = _tm.load_floors("cal-demo")
            assert fl["r1"] == payload["floors"]["r1"]
            assert meta["rounds"] == 3
        finally:
            del os.environ["ARO_FLOORS_DIR"]

    # CLI dry-run: clean without measure binary
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        spec_path = d / "cal.json"
        spec_path.write_text(json.dumps({
            "name": "cal-smoke",
            "target_repo": {"path": str(d / "no-repo")},
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "metric": "ns_per_call",
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "terminal_bench_targets": ["mega_bench"],
            "icount_epsilon_pct": 0.1,
        }))
        buf = io.StringIO()
        with redirect_stdout(buf):
            _tm.calibrate_cli(SimpleNamespace(
                spec=str(spec_path), checkout=str(d / "wt"),
                rounds=4, dry_run=True, measure_bin=None))
        out = buf.getvalue()
        assert "terminal-calibrate dry-run" in out, out
        assert "rounds:    4" in out
        assert "would write floors" in out
        assert "MEASURE_BIN" in out or "UNSET" in out

        # missing measure_bin on real calibrate path → clear error, not traceback.
        # Skip selfcheck so this smoke still exercises the measure_bin error
        # (selfcheck hard-error is covered in case_33).
        err = io.StringIO()
        os.environ["ARO_SKIP_SELFCHECK"] = "1"
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                _tm.calibrate_cli(SimpleNamespace(
                    spec=str(spec_path), checkout=str(d / "wt"),
                    rounds=2, dry_run=False, measure_bin=None))
            assert False, "missing measure_bin must SystemExit"
        except SystemExit as se:
            assert se.code == 2
        finally:
            del os.environ["ARO_SKIP_SELFCHECK"]
        assert "measure binary unset" in err.getvalue() or \
               "ARO_MEASURE_BIN" in err.getvalue()

        # argparse wires terminal-calibrate
        p = _cli.build_parser()
        a = p.parse_args([
            "terminal-calibrate", str(spec_path),
            "--checkout", str(d / "wt"), "--rounds", "3", "--dry-run",
        ])
        assert a.cmd == "terminal-calibrate"
        assert a.rounds == 3 and a.dry_run is True
    print("#43e OK: calibrate runner + CLI dry-run / missing-bin")
    print("#43 OK: terminal floors + median-of-N (T8)")


def case_33():
    """T9: selfcheck gate + env_fingerprint (hermetic)."""
    import importlib
    import io
    import os
    import subprocess as _sp
    from contextlib import redirect_stderr
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from aro import selfcheck as _sc
    from aro import terminal as _tm
    from aro import lessons as _les
    from aro import permtree as _pt
    from aro import cli as _cli
    from aro.icount import ICountResult

    # --- version probe parsing + cargo-codspeed exit-1 quirk -----------------
    def _vrunner(cmd):
        c0 = cmd[0] if cmd else ""
        if c0 == "codspeed":
            return "codspeed 4.18.3\n"
        if c0 == "cargo" and "codspeed" in cmd:
            # clap quirk: nonzero exit while printing banner — runner returns text only
            return "cargo-codspeed 5.0.1\nerror: unrecognized subcommand\n"
        if c0 == "valgrind":
            return "valgrind-3.26.0.codspeed5\n"
        if c0 == "rustc":
            return "rustc 1.80.0 (aaa 2024-01-01)\n"
        return ""

    vers = _sc.probe_tool_versions(runner=_vrunner, use_cache=False)
    assert vers["codspeed"] == "4.18.3", vers
    assert vers["cargo-codspeed"] == "5.0.1", vers
    assert "3.26.0" in vers["valgrind"], vers
    assert vers["rustc"].startswith("1.80.0"), vers
    fp = _sc.env_fingerprint(vers)
    assert fp == ("codspeed=4.18.3;cargo-codspeed=5.0.1;"
                  "valgrind=3.26.0.codspeed5;rustc=1.80.0"), fp
    # missing tools → unknown
    empty = _sc.env_fingerprint(
        {"codspeed": "", "cargo-codspeed": "", "valgrind": "", "rustc": ""})
    assert empty == ("codspeed=unknown;cargo-codspeed=unknown;"
                     "valgrind=unknown;rustc=unknown")
    # no version-like token (cargo 'error: no such command') → 'unknown', never raw
    assert _sc._first_version_token(
        "error: no such command: `codspeed`\n") == "unknown"
    assert _sc._first_version_token(
        "error: no such command\n") == "unknown"
    assert "error" not in _sc._first_version_token(
        "error: no such command: `codspeed`\n")
    # tool banners naming themselves 'codspeed-runner' (the real 4.18.3 CLI
    # shape) must yield the version, not the 'runner' name fragment
    assert _sc._first_version_token("codspeed-runner 4.18.3\n") == "4.18.3"
    vers_runner = _sc.probe_tool_versions(
        runner=lambda cmd: ("codspeed-runner 4.18.3\n"
                            if cmd and cmd[0] == "codspeed" else _vrunner(cmd)),
        use_cache=False)
    assert vers_runner["codspeed"] == "4.18.3", vers_runner
    print("#44a OK: version probe parse + cargo-codspeed exit-1 + fingerprint")

    # --- pin check (exact / token-boundary; not bidirectional substring) -----
    assert _sc.check_pinned_tools(vers, {}) is None
    assert _sc.check_pinned_tools(vers, {
        "codspeed": "4.18.3", "valgrind": "3.26.0.codspeed5"}) is None
    # build-tag suffix still matches via token-boundary prefix
    assert _sc.check_pinned_tools(
        {"valgrind": "3.26.0.codspeed5"}, {"valgrind": "3.26.0"}) is None
    pin_err = _sc.check_pinned_tools(vers, {"codspeed": "9.9.9"})
    assert pin_err and "expected" in pin_err and "9.9.9" in pin_err
    # pin '3.26.0' must NOT match found '13.26.0' (old bidirectional substring bug)
    pin_mid = _sc.check_pinned_tools(
        {"valgrind": "13.26.0"}, {"valgrind": "3.26.0"})
    assert pin_mid and "3.26.0" in pin_mid and "13.26.0" in pin_mid, pin_mid
    print("#44b OK: pin check match / mismatch / no mid-token false positive")

    # --- process cache: only SUCCESSFUL probes are cached --------------------
    # Exercise the real cache path (runner=None, no override) by patching
    # `_run_version_cmd`: first probe fails transiently → not cached; second
    # succeeds and is cached; third hits the cache.
    _sc.clear_version_cache()
    _sc.set_version_runner(None)
    state = {"phase": "fail"}
    orig_run_version = _sc._run_version_cmd

    def _phase_cmd(cmd, timeout=15.0):
        if state["phase"] == "fail":
            return ""
        return _vrunner(list(cmd))

    _sc._run_version_cmd = _phase_cmd
    try:
        v1 = _sc.probe_tool_versions(use_cache=True)
        assert not _sc._probe_succeeded(v1), v1
        assert _sc._VERSION_CACHE is None, "failure must not be cached"
        state["phase"] = "ok"
        v2 = _sc.probe_tool_versions(use_cache=True)
        assert _sc._probe_succeeded(v2), v2
        assert _sc._VERSION_CACHE is not None, "success must be cached"
        # Third call must hit cache (phase flip would break it if re-probed)
        state["phase"] = "fail"
        v3 = _sc.probe_tool_versions(use_cache=True)
        assert v3["codspeed"] == v2["codspeed"] == "4.18.3"
    finally:
        _sc._run_version_cmd = orig_run_version
        _sc.clear_version_cache()
    print("#44b2 OK: version cache stores only successful probes")

    # --- skip_selfcheck_requested: only 1/true/yes (case-insensitive) --------
    for truthy in ("1", "true", "TRUE", "Yes", "yes"):
        os.environ["ARO_SKIP_SELFCHECK"] = truthy
        assert _sc.skip_selfcheck_requested() is True, truthy
    for falsy in ("", "0", "false", "no", "off", "maybe", "2", "TRUEISH"):
        if falsy == "":
            os.environ.pop("ARO_SKIP_SELFCHECK", None)
        else:
            os.environ["ARO_SKIP_SELFCHECK"] = falsy
        assert _sc.skip_selfcheck_requested() is False, repr(falsy)
    os.environ.pop("ARO_SKIP_SELFCHECK", None)
    print("#44b3 OK: skip_selfcheck_requested only 1/true/yes")

    # --- same_binary_spread_pct: two zeros → measurement error, not 0% pass --
    try:
        _sc.same_binary_spread_pct(0, 0)
        assert False, "two zero Ir readings must raise (measurement error)"
    except ValueError as e:
        assert "zero" in str(e).lower() or "measurement" in str(e).lower()
    assert abs(_sc.same_binary_spread_pct(10000, 10040) - 0.3996) < 1e-3
    print("#44b4 OK: same_binary_spread_pct rejects dual-zero Ir")

    # --- marker lifecycle: pass writes; fail no marker -----------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_RUNS_ROOT"] = str(td)
        _sc.clear_version_cache()
        try:
            mpath = td / "selfcheck" / "demo.json"
            # fail: spread too high → no marker
            irs = iter([
                ICountResult(ir=10000, events={"Ir": 10000}),
                ICountResult(ir=11000, events={"Ir": 11000}),  # ~9.5%
            ])

            def _noisy(work, scale=1, cache_sim=False):
                return next(irs)

            sp = SimpleNamespace(
                name="demo", selfcheck_probe_max_pct=0.05, raw={}, pinned_tools=None)
            r = _sc.run_selfcheck(
                sp, icount_fn=_noisy, make_worktree=False,
                version_runner=_vrunner, marker_path_override=mpath)
            assert r.ok is False
            assert r.probe_spread_pct is not None and r.probe_spread_pct > 0.05
            assert not mpath.exists(), "fail must not write marker"

            # fail: pin mismatch → no marker
            sp_pin = SimpleNamespace(
                name="demo", selfcheck_probe_max_pct=1.0, raw={},
                pinned_tools={"codspeed": "0.0.1"})
            irs2 = iter([
                ICountResult(ir=10000, events={"Ir": 10000}),
                ICountResult(ir=10000, events={"Ir": 10000}),
            ])
            r = _sc.run_selfcheck(
                sp_pin, icount_fn=lambda *a, **k: next(irs2),
                make_worktree=False, version_runner=_vrunner,
                marker_path_override=mpath)
            assert r.ok is False
            assert any("pin" in n.lower() for n in r.notes)
            assert not mpath.exists()

            # pass: tight A/A → marker written
            irs3 = iter([
                ICountResult(ir=1000000, events={"Ir": 1000000}),
                ICountResult(ir=1000040, events={"Ir": 1000040}),  # 0.004%
            ])
            r = _sc.run_selfcheck(
                sp, icount_fn=lambda *a, **k: next(irs3),
                make_worktree=False, version_runner=_vrunner,
                marker_path_override=mpath)
            assert r.ok is True, r.notes
            assert mpath.is_file()
            marker = json.loads(mpath.read_text())
            assert "passed_at" in marker
            assert marker["env_fingerprint"] == fp
            assert marker["rounds"] == 2
            assert abs(marker["probe_spread_pct"] - r.probe_spread_pct) < 1e-9
        finally:
            del os.environ["ARO_RUNS_ROOT"]
            _sc.clear_version_cache()
    print("#44c OK: marker lifecycle pass/fail (spread + pin)")

    # --- gate integration: missing / stale / mismatch / skip / valid ---------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_RUNS_ROOT"] = str(td)
        _sc.clear_version_cache()
        try:
            sp = SimpleNamespace(name="gate-demo", raw={})
            # missing marker → hard error
            try:
                _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
                assert False, "missing marker must raise"
            except _sc.SelfcheckError as e:
                assert "no marker" in str(e).lower() or "selfcheck" in str(e).lower()
                assert "selfcheck" in str(e)

            # write a valid marker then pass
            mpath = _sc.marker_path("gate-demo")
            _sc.write_marker("gate-demo", env_fp=fp, probe_spread_pct=0.004)
            got = _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
            assert got == fp

            # stale (>14d) → hard error
            stale_at = (datetime.now(timezone.utc) - timedelta(days=20)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            mpath.write_text(json.dumps({
                "passed_at": stale_at, "env_fingerprint": fp,
                "probe_spread_pct": 0.004, "rounds": 2,
            }) + "\n")
            try:
                _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
                assert False, "stale marker must raise"
            except _sc.SelfcheckError as e:
                assert "days" in str(e) or "old" in str(e)

            # fingerprint mismatch → hard error
            _sc.write_marker(
                "gate-demo",
                env_fp="codspeed=0;cargo-codspeed=0;valgrind=0;rustc=0",
                probe_spread_pct=0.001)
            try:
                _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
                assert False, "fp mismatch must raise"
            except _sc.SelfcheckError as e:
                assert "mismatch" in str(e).lower() or "fingerprint" in str(e)

            # ARO_SKIP_SELFCHECK=1 → proceeds with warning, NO version probe
            # (returns None; skip-when-absent — never a fresh fingerprint).
            _sc.write_marker(  # leave a mismatched marker; skip must ignore it
                "gate-demo",
                env_fp="codspeed=0;cargo-codspeed=0;valgrind=0;rustc=0",
                probe_spread_pct=0.001)
            os.environ["ARO_SKIP_SELFCHECK"] = "1"
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    got = _sc.require_selfcheck(
                        sp, runner=_vrunner, use_cache=False)
                assert got is None, "skip must not return a fingerprint"
                assert "ARO_SKIP_SELFCHECK" in err.getvalue()
                assert "WARNING" in err.getvalue()
            finally:
                del os.environ["ARO_SKIP_SELFCHECK"]

            # Hermeticity: under skip, subprocess.run must never fire.
            os.environ["ARO_SKIP_SELFCHECK"] = "1"
            real_run = _sp.run

            def _boom(*a, **k):
                raise AssertionError(
                    "subprocess.run fired under ARO_SKIP_SELFCHECK — "
                    "skip must short-circuit BEFORE version probing")

            _sp.run = _boom
            try:
                with redirect_stderr(io.StringIO()):
                    got = _sc.require_selfcheck(sp)  # no runner inject
                assert got is None
            finally:
                _sp.run = real_run
                del os.environ["ARO_SKIP_SELFCHECK"]

            # terminal gate honors marker (missing → TerminalError)
            # version_runner keeps this path hermetic (no real tool probes).
            del os.environ["ARO_RUNS_ROOT"]  # use a fresh empty root
            os.environ["ARO_RUNS_ROOT"] = str(td / "empty-runs")
            tsp = SimpleNamespace(
                name="term-sc",
                terminal_bench_targets=["mega_bench"],
                terminal_bench_filter=None,
                measure_bin="/fake/reporter",
                icount_epsilon_pct=0.1,
                timeout=1800,
                bench={"pkg": "p"},
                raw={},
            )

            def _runner(cmd, timeout=None):
                body = {"rows": {"row": {"instr_count": 10000}},
                        "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
                return json.dumps(body), "", 0

            try:
                _tm.run_terminal(
                    tsp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
                    rounds=1, floors={}, version_runner=_vrunner)
                assert False, "terminal without marker must hard-error"
            except _tm.TerminalError as e:
                assert "selfcheck" in str(e).lower()

            # with skip_selfcheck=True hermetic path still works + loud warning
            err2 = io.StringIO()
            with redirect_stderr(err2):
                r = _tm.run_terminal(
                    tsp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
                    rounds=1, floors={}, skip_selfcheck=True)
            assert r.verdict == _tm.TERMINAL_UNTOUCHED
            assert "WARNING" in err2.getvalue()
            assert "skip_selfcheck=True" in err2.getvalue()
            # skip-when-absent: no env_fingerprint when selfcheck was skipped
            assert not r.env_fingerprint

            # run_calibrate under skip omits env_fingerprint (no fresh probe)
            with tempfile.TemporaryDirectory() as floord:
                os.environ["ARO_FLOORS_DIR"] = floord
                try:
                    seq = [
                        {"rows": {"r": {"instr_count": 10000}},
                         "meta": {"profile_fingerprint": "fp", "rustc": "r"}},
                        {"rows": {"r": {"instr_count": 10010}},
                         "meta": {"profile_fingerprint": "fp", "rustc": "r"}},
                    ]
                    it = iter(seq)

                    def _cal_r(cmd, timeout=None):
                        return json.dumps(next(it)), "", 0

                    with redirect_stderr(io.StringIO()):
                        payload = _tm.run_calibrate(
                            tsp, "/tmp/checkout-wt", rounds=2,
                            runner=_cal_r, skip_selfcheck=True)
                    assert "env_fingerprint" not in payload["meta"], payload["meta"]
                finally:
                    del os.environ["ARO_FLOORS_DIR"]
        finally:
            os.environ.pop("ARO_RUNS_ROOT", None)
            os.environ.pop("ARO_SKIP_SELFCHECK", None)
            os.environ.pop("ARO_FLOORS_DIR", None)
            _sc.clear_version_cache()
            _sc.set_version_runner(None)
    print("#44d OK: gate integration missing/stale/mismatch/skip/valid")

    # --- records: env_fingerprint additive; absent-field byte-compat ---------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        os.environ["ARO_PERMTREE_DIR"] = str(d / "pt")
        importlib.reload(_pt)
        orig = _les._PATH
        _les._PATH = d / "lessons.jsonl"
        try:
            _les.append("t", "cpu win", "accepted-ir", delta_pct=-2.5,
                        note="Ir gate", ir_delta_pct=-2.5,
                        profile_fingerprint="rustc 1.80|deadbeef",
                        env_fingerprint=fp, backend="codex/gpt-5.2")
            row = json.loads(_les._PATH.read_text().splitlines()[0])
            assert row["env_fingerprint"] == fp
            assert row["profile_fingerprint"] == "rustc 1.80|deadbeef"
            assert row["backend"] == "codex/gpt-5.2"
            # non-icount path: no extra keys
            _les.append("t", "old path", "within-noise", delta_pct=0.1, note="aa")
            row2 = json.loads(_les._PATH.read_text().splitlines()[1])
            assert "env_fingerprint" not in row2
            assert "profile_fingerprint" not in row2
            assert "backend" not in row2

            rec = _pt.record(
                "spec-x", workload="spec-x", fn="hot", base_state="origin",
                verdict="accepted-ir", regime="strict", delta=-2.5,
                env_fingerprint=fp, profile_fingerprint="rustc|abc",
                backend="codex/gpt-5.2")
            assert rec["env_fingerprint"] == fp
            assert rec["backend"] == "codex/gpt-5.2"
            rec2 = _pt.record(
                "spec-x", workload="spec-x", fn="cold", base_state="origin",
                verdict="within-noise", regime="strict", delta=0.0)
            assert "env_fingerprint" not in rec2
            assert "backend" not in rec2

            # terminal record carries env_fingerprint
            res = _tm.TerminalResult(
                verdict=_tm.TERMINAL_CONFIRMED,
                bench_ir_rows={"r": -1.0},
                profile_fingerprint="fp-abc",
                env_fingerprint=fp,
                notes=["ok"],
            )
            ddict = res.to_dict()
            assert ddict["env_fingerprint"] == fp
            # empty env_fingerprint omitted from to_dict (byte-compat)
            res2 = _tm.TerminalResult(
                verdict=_tm.TERMINAL_UNTOUCHED,
                bench_ir_rows={},
                profile_fingerprint="fp-abc",
            )
            assert "env_fingerprint" not in res2.to_dict()
            _tm.record_terminal("spec-x", res, fn="hot")
            les = [json.loads(l) for l in _les._PATH.read_text().splitlines()]
            assert any(r.get("env_fingerprint") == fp for r in les)
        finally:
            _les._PATH = orig
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#44e OK: env_fingerprint/backend additive on lessons/permtree; terminal unchanged")

    # --- --rows row-set integrity (not row-level A/A) ------------------------
    warns = _sc.check_row_set_integrity(
        {"a": 1, "b": 2}, {"a": 0.1, "b": 0.2, "c": 0.3})
    assert any("missing" in w for w in warns)
    warns = _sc.check_row_set_integrity({"a": 1, "b": 2}, {"a": 0.1, "b": 0.2})
    assert any("OK" in w for w in warns)
    warns = _sc.check_row_set_integrity({"a": 1}, {})
    assert any("no calibrated" in w for w in warns)

    # CLI argparse wires selfcheck + --rows
    p = _cli.build_parser()
    a = p.parse_args(["selfcheck", "targets/x.json", "--rows"])
    assert a.cmd == "selfcheck" and a.rows is True
    a2 = p.parse_args(["selfcheck", "targets/x.json"])
    assert a2.rows is False
    print("#44f OK: --rows integrity + CLI wiring")
    print("#44 OK: selfcheck gate + env_fingerprint (T9)")


def case_34():
    # --- #45: multi-backend LLM command construction (hermetic; no CLI spawn) ---
    from aro import llm as _llm

    old_bins = (_llm.CLAUDE_BIN, _llm.CODEX_BIN, _llm.GROK_BIN)
    old_fallback = _llm.CLAUDE_FALLBACK_MODELS
    old_codex_sandbox = _llm.ARO_CODEX_SANDBOX
    _llm.CLAUDE_BIN = "claude-test"
    _llm.CODEX_BIN = "codex-test"
    _llm.GROK_BIN = "grok-test"
    _llm.CLAUDE_FALLBACK_MODELS = "sonnet"
    try:
        claude = _llm.get_backend("claude")
        assert claude.build_cmd("hello", "/tmp/work", False, 600) == [
            "claude-test", "--fallback-model", "sonnet",
            "--output-format", "json", "-p", "hello"]
        assert claude.build_cmd("hello", "/tmp/work", True, 600) == [
            "claude-test", "--dangerously-skip-permissions",
            "--fallback-model", "sonnet", "--output-format", "json", "-p", "hello"]

        codex = _llm.get_backend("codex")
        assert codex.build_cmd("hello", "/tmp/work", False, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox", "read-only",
            "--json", "hello"]
        assert codex.build_cmd("hello", "/tmp/work", True, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox", "workspace-write",
            "--json", "hello"]

        # ARO_CODEX_SANDBOX escape hatch: write tier follows the override
        # (normalized), read tier stays pinned to read-only.
        _llm.ARO_CODEX_SANDBOX = " Danger-Full-Access "
        assert codex.build_cmd("hello", "/tmp/work", True, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox",
            "danger-full-access", "--json", "hello"]
        assert codex.build_cmd("hello", "/tmp/work", False, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox", "read-only",
            "--json", "hello"]
        assert codex.write_sandbox == "danger-full-access"
        _llm.ARO_CODEX_SANDBOX = "yolo"
        try:
            codex.build_cmd("hello", "/tmp/work", True, 600)
            raise AssertionError("invalid ARO_CODEX_SANDBOX must raise")
        except _llm.LLMError as e:
            msg = str(e)
            assert all(s in msg for s in
                       ("yolo", "workspace-write", "danger-full-access")), msg
        # an invalid value must not break read-only calls (critic path)
        assert codex.build_cmd("hello", "/tmp/work", False, 600)[5] == "read-only"
        _llm.ARO_CODEX_SANDBOX = ""
        assert codex.write_sandbox == "workspace-write"

        grok = _llm.get_backend("grok")
        assert grok.build_cmd("hello", "/tmp/work", False, 600) == [
            "grok-test", "-p", "hello", "--cwd", "/tmp/work",
            "--output-format", "json", "--max-turns", "100",
            "--sandbox", "aro-read-only"]
        assert grok.build_cmd("hello", "/tmp/work", True, 600) == [
            "grok-test", "-p", "hello", "--cwd", "/tmp/work",
            "--output-format", "json", "--max-turns", "100",
            "--sandbox", "aro-workspace", "--always-approve"]

        assert claude.parse_reply(
            '{"result":"claude-answer","usage":{"output_tokens":5},'
            '"total_cost_usd":0.25}', "", 0) == ("claude-answer", 5, 0.25)
        assert _llm.parse_json_reply("legacy plain reply") == (
            "legacy plain reply", 0, 0.0)
        codex_jsonl = "\n".join([
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"item.completed","item":{"id":"i0",'
            '"type":"agent_message","text":"working"}}',
            '{"type":"item.completed","item":{"id":"i1",'
            '"type":"agent_message","text":"codex-answer"}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,'
            '"cached_input_tokens":2,"output_tokens":3}}',
        ])
        assert codex.parse_reply(codex_jsonl, "", 0) == ("codex-answer", 3, None)
        assert grok.parse_reply(
            '{"text":"grok-answer","usage":{"output_tokens":7},'
            '"total_cost_usd":0.125}', "", 0) == ("grok-answer", 7, 0.125)

        for backend in (claude, codex, grok):
            try:
                backend.parse_reply("not-json", "", 0)
                raise AssertionError(f"{backend.name} malformed reply must raise")
            except _llm.LLMError:
                pass
            try:
                backend.parse_reply("{}", "backend failed", 7)
                raise AssertionError(f"{backend.name} nonzero reply must raise")
            except _llm.LLMError as e:
                assert "7" in str(e) and "backend failed" in str(e), e
        for backend, malformed in (
                (claude, '{"result":"ok","usage":[]}'),
                (codex, '{"type":"item.completed","item":[]}'),
                (grok, '{"text":"ok","usage":[]}')):
            try:
                backend.parse_reply(malformed, "", 0)
                raise AssertionError(f"{backend.name} malformed shape must raise")
            except _llm.LLMError:
                pass
        try:
            codex.parse_reply('{"type":"turn.completed","usage":{}}', "", 0)
            raise AssertionError("codex reply without an agent message must raise")
        except _llm.LLMError:
            pass

        old_selected = _llm.os.environ.pop("ARO_LLM_BACKEND", None)
        try:
            spec_cfg = _types.SimpleNamespace(llm_backend="codex", critic_backend=None)
            assert _llm.select_backend().name == "claude"
            assert _llm.select_backend(spec_cfg).name == "codex"
            _llm.os.environ["ARO_LLM_BACKEND"] = "grok"
            assert _llm.select_backend(spec_cfg).name == "grok"
            cross_model = _types.SimpleNamespace(
                llm_backend="claude", critic_backend="codex")
            assert _llm.select_backend(cross_model, critic=True).name == "codex"
            del _llm.os.environ["ARO_LLM_BACKEND"]
            try:
                _llm.select_backend(
                    _types.SimpleNamespace(llm_backend="mystery", critic_backend=None))
                raise AssertionError("unknown backend must raise")
            except _llm.LLMError as e:
                msg = str(e)
                assert all(s in msg for s in ("mystery", "claude", "codex", "grok")), msg
        finally:
            _llm.os.environ.pop("ARO_LLM_BACKEND", None)
            if old_selected is not None:
                _llm.os.environ["ARO_LLM_BACKEND"] = old_selected

        runtime_selected = _llm.os.environ.pop("ARO_LLM_BACKEND", None)
        try:
            from aro import critic as _critic, generator as _generator
            target = _types.SimpleNamespace(
                spec=_types.SimpleNamespace(llm_backend="codex", critic_backend=None),
                repo=Path("/tmp/work"))
            assert _generator.RalphGenerator(target).backend.name == "codex"
            assert _generator.AgenticGenerator(target).backend.name == "codex"
            with tempfile.TemporaryDirectory() as agent_tmp:
                scratch = Path(agent_tmp).resolve()
                cargo_td = Path(_generator._agent_env(scratch)["CARGO_TARGET_DIR"])
                assert cargo_td.parent == scratch and cargo_td.name == ".aro-agent-target"

            critic_calls = []
            old_run_llm = _llm.run_llm

            def fake_llm(prompt, **kwargs):
                critic_calls.append(kwargs["backend"].name)
                return '{"verdict":"pass","reasons":[]}', 9, None

            _llm.run_llm = fake_llm
            try:
                critique = _critic.critique(
                    "code", "candidate", backend=_llm.get_backend("grok"))
                assert critique.verdict == "pass" and critique.tokens == 9
                assert critic_calls == ["grok"], critic_calls
            finally:
                _llm.run_llm = old_run_llm
        finally:
            if runtime_selected is not None:
                _llm.os.environ["ARO_LLM_BACKEND"] = runtime_selected
    finally:
        _llm.CLAUDE_BIN, _llm.CODEX_BIN, _llm.GROK_BIN = old_bins
        _llm.CLAUDE_FALLBACK_MODELS = old_fallback
        _llm.ARO_CODEX_SANDBOX = old_codex_sandbox

    import importlib as _importlib
    override_names = ("ARO_CLAUDE_BIN", "ARO_CODEX_BIN", "ARO_GROK_BIN")
    old_overrides = {name: _llm.os.environ.get(name) for name in override_names}
    try:
        for name, value in zip(override_names, ("c-env", "x-env", "g-env")):
            _llm.os.environ[name] = value
        _importlib.reload(_llm)
        assert _llm.get_backend("claude").build_cmd("p", "/w", False, 1)[0] == "c-env"
        assert _llm.get_backend("codex").build_cmd("p", "/w", False, 1)[0] == "x-env"
        assert _llm.get_backend("grok").build_cmd("p", "/w", False, 1)[0] == "g-env"
    finally:
        for name, value in old_overrides.items():
            if value is None:
                _llm.os.environ.pop(name, None)
            else:
                _llm.os.environ[name] = value
        _importlib.reload(_llm)
    print("#45a-e OK: commands/parsing/config/topology + binary env overrides")


def case_35():
    """T13b: terminal-gate full-suite correctness tier (test_full).

    Hermetic — injects test_full_runner + measure runner; never spawns cargo.
    (a) absent test_full → byte-identical (no runner invocation)
    (b) declared + exit 0 → measure proceeds, verdict unaffected
    (c) declared + exit 1 → TERMINAL_TEST_FAILED, no measure, output tail
    (d) verdict survives terminal.json round-trip; apply_terminal keeps
        mergeable=false for every entry
    """
    print("=== case 35: terminal test_full correctness tier ===")
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import manifest as _mf

    assert _tm.TERMINAL_TEST_FAILED in _tm.ALL_TERMINAL_VERDICTS
    from aro import permtree as _pt
    from aro.attempt import _VERDICT_RANK
    assert _tm.TERMINAL_TEST_FAILED in _pt._CLOSED_VERDICTS
    assert _tm.TERMINAL_TEST_FAILED in _VERDICT_RANK

    def _mk_spec(**raw_extra):
        raw = dict(raw_extra)
        return SimpleNamespace(
            name="term-tf",
            terminal_bench_targets=["mega_bench"],
            terminal_bench_filter=None,
            measure_bin="/fake/reporter",
            icount_epsilon_pct=0.1,
            timeout=1800,
            bench={"pkg": "mega-evm"},
            raw=raw,
        )

    def _measure_runner_factory(calls):
        def _runner(cmd, timeout=None):
            calls.append(list(cmd))
            checkout = cmd[cmd.index("--checkout") + 1]
            if "base" in checkout:
                body = {"rows": {"row": {"instr_count": 10000}},
                        "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
            else:
                body = {"rows": {"row": {"instr_count": 9000}},
                        "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
            return json.dumps(body), "", 0
        return _runner

    # (a) absent test_full → no runner invocation; measure still runs
    measure_calls = []
    tf_calls = []

    def _tf_a(cmd, *, cwd, timeout=None):
        tf_calls.append((list(cmd), str(cwd), timeout))
        return "", "", 0

    sp_a = _mk_spec()  # no correctness_oracle.test_full
    assert _tm.resolve_test_full(sp_a) is None
    r_a = _tm.run_terminal(
        sp_a, "/tmp/base-wt", "/tmp/cand-wt",
        runner=_measure_runner_factory(measure_calls),
        test_full_runner=_tf_a,
        rounds=1, floors={}, skip_selfcheck=True)
    assert r_a.verdict == _tm.TERMINAL_CONFIRMED, r_a
    assert tf_calls == [], f"absent test_full must not invoke runner: {tf_calls}"
    assert len(measure_calls) == 2
    print("#46a OK: absent test_full → no runner, measure proceeds")

    # (b) declared + exit 0 → measure proceeds, verdict unaffected
    measure_calls.clear()
    tf_calls.clear()

    def _tf_ok(cmd, *, cwd, timeout=None):
        tf_calls.append((list(cmd), str(cwd), timeout))
        return "test result: ok. 12 passed\n", "", 0

    sp_b = _mk_spec(correctness_oracle={
        "build": ["cargo", "build"],
        "test": ["cargo", "test", "--lib"],
        "test_full": ["cargo", "test", "--release", "-p", "mega-evm"],
    })
    assert _tm.resolve_test_full(sp_b) == [
        "cargo", "test", "--release", "-p", "mega-evm"]
    assert _tm.resolve_test_full_timeout(sp_b) == 1800.0
    r_b = _tm.run_terminal(
        sp_b, "/tmp/base-wt", "/tmp/cand-wt",
        runner=_measure_runner_factory(measure_calls),
        test_full_runner=_tf_ok,
        rounds=1, floors={}, skip_selfcheck=True)
    assert r_b.verdict == _tm.TERMINAL_CONFIRMED, r_b
    assert len(tf_calls) == 1
    assert tf_calls[0][0] == ["cargo", "test", "--release", "-p", "mega-evm"]
    assert "cand" in tf_calls[0][1]  # candidate_dir only
    assert tf_calls[0][2] == 1800.0
    assert len(measure_calls) == 2
    print("#46b OK: test_full exit 0 → measure proceeds, CONFIRMED")

    # (c) declared + exit 1 → TERMINAL_TEST_FAILED, no measure, output tail
    measure_calls.clear()
    tf_calls.clear()
    long_out = ("ok line\n" * 50) + ("FAIL: semantics_diff\n" * 100)
    assert len(long_out) > 2000

    def _tf_fail(cmd, *, cwd, timeout=None):
        tf_calls.append((list(cmd), str(cwd), timeout))
        return long_out, "error: test failed\n", 1

    r_c = _tm.run_terminal(
        sp_b, "/tmp/base-wt", "/tmp/cand-wt",
        runner=_measure_runner_factory(measure_calls),
        test_full_runner=_tf_fail,
        rounds=1, floors={}, skip_selfcheck=True)
    assert r_c.verdict == _tm.TERMINAL_TEST_FAILED, r_c
    assert measure_calls == [], f"must not measure after test fail: {measure_calls}"
    assert len(tf_calls) == 1
    assert any("TERMINAL_TEST_FAILED" in n for n in r_c.notes)
    # last ~2000 chars of combined output are retained
    joined = "\n".join(r_c.notes)
    assert "FAIL: semantics_diff" in joined
    assert len(joined) < len(long_out) + 500  # tail capped, not full dump
    assert r_c.bench_ir_rows == {}
    assert r_c.rounds == 0
    print("#46c OK: test_full exit 1 → TERMINAL_TEST_FAILED, no measure, tail kept")

    # (d) terminal.json round-trip + apply_terminal keeps mergeable=false
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def J(o):
            return json.dumps(o)

        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
            # second accepted entry so "every entry" is non-trivial
            {"event": "attempt_started", "run_id": "R", "fn": "sstore",
             "regime": "byte-identical", "files": ["crates/x/src/c.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-1",
             "hypothesis": "inline"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-1", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-1",
             "deltas": [{"metric": "ns", "delta_pct": -2.0, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-1"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        for cid, fname in (("agent-r0-0", "b.rs"), ("agent-r0-1", "c.rs")):
            pd = d / "a1" / "patches"
            pd.mkdir(parents=True, exist_ok=True)
            (pd / f"{cid}.txt").write_text(
                f"--- edit 1 ---\npath: crates/x/src/{fname}\n"
                "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")

        # Write terminal.json via TerminalResult.to_dict (CLI --out path)
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(r_c.to_dict(), ensure_ascii=False, indent=1) + "\n")
        loaded = json.loads(tpath.read_text())
        assert loaded["verdict"] == "TERMINAL_TEST_FAILED"
        assert any("TERMINAL_TEST_FAILED" in n for n in loaded["notes"])
        # notes carry the output tail through the round-trip
        assert "FAIL: semantics_diff" in "\n".join(loaded["notes"])

        m = _mf.build_manifest(d, terminal_required=True)
        assert all(a["mergeable"] is False for a in m["accepted"])
        m = _mf.apply_terminal(m, loaded, terminal_required=True)
        assert m["terminal"]["verdict"] == "TERMINAL_TEST_FAILED"
        assert len(m["accepted"]) >= 2
        for a in m["accepted"]:
            assert a["terminal"] == "TERMINAL_TEST_FAILED", a
            assert a["mergeable"] is False, a
        # is_mergeable itself rejects the new verdict
        assert _mf.is_mergeable(
            "byte-identical", "pass",
            terminal=_tm.TERMINAL_TEST_FAILED,
            terminal_required=True) is False

        # test_full_timeout_secs override
        sp_to = _mk_spec(
            correctness_oracle={"test_full": ["true"]},
            test_full_timeout_secs=99)
        assert _tm.resolve_test_full_timeout(sp_to) == 99.0
    print("#46d OK: terminal.json round-trip + apply_terminal mergeable=false")
    print("case 35 OK")


def case_36():
    """T14: lane-aware terminal verdict + offline re-judge.

    Hermetic — pure judge_terminal / rejudge_terminal_doc / apply_terminal.
    (a) mixed subject+control within bound → CONFIRMED, controls control-ok
    (b) control beyond bound → TERMINAL_CONTROL_ANOMALY even if subjects improved
    (c) no control_lanes → byte-identical legacy statuses/verdict
    (d) segment-exact matching (not substring)
    (e) re-judge round-trip: verdict flips, input unmodified, note appended
    (f) apply_terminal keeps mergeable=false under TERMINAL_CONTROL_ANOMALY
    """
    print("=== case 36: lane-aware terminal + rejudge ===")
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import manifest as _mf
    from aro import permtree as _pt
    from aro.attempt import _VERDICT_RANK

    assert _tm.TERMINAL_CONTROL_ANOMALY in _tm.ALL_TERMINAL_VERDICTS
    assert _tm.TERMINAL_CONTROL_ANOMALY in _pt._CLOSED_VERDICTS
    assert _tm.TERMINAL_CONTROL_ANOMALY in _VERDICT_RANK

    CONTROL = ["revm_pinned", "revm_latest", "op_revm_pinned", "op_revm_latest"]
    BOUND = 2.0

    def _doc(rows, fp="fp-lane"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    # (d) segment matching first — pure function, no judge
    assert _tm.is_control_row(
        "log_opcodes/op_revm_latest/log4_32b", CONTROL) is True
    assert _tm.is_control_row(
        "system_contract_100x/rex4/limit_control", CONTROL) is False
    assert _tm.is_control_row(
        "revm_pinned_x/rex5/case", CONTROL) is False  # not exact segment
    assert _tm.is_control_row(
        "group/revm_pinned/case", CONTROL) is True
    assert _tm.is_control_row("a/b/c", []) is False
    print("#47d OK: segment-exact control-lane matching")

    # (a) subject improved + control within bound → CONFIRMED
    r_a = _tm.judge_terminal(
        _doc({
            "log_opcodes/rex4/log4_32b": 10000,
            "log_opcodes/op_revm_latest/log4_32b": 20000,
            "system_contract_100x/rex5/case": 5000,
            "group/revm_pinned/case": 8000,
        }),
        _doc({
            "log_opcodes/rex4/log4_32b": 9000,       # -10% subject improved
            "log_opcodes/op_revm_latest/log4_32b": 20100,  # +0.5% control-ok
            "system_contract_100x/rex5/case": 5000,  # 0 subject untouched
            "group/revm_pinned/case": 8100,          # +1.25% control-ok
        }),
        epsilon_pct=0.1,
        control_lanes=CONTROL,
        control_composition_bound_pct=BOUND,
    )
    assert r_a.verdict == _tm.TERMINAL_CONFIRMED, r_a
    by_key = {rd.row_key: rd for rd in r_a.rows}
    assert by_key["log_opcodes/rex4/log4_32b"].status == "improved"
    assert by_key["log_opcodes/op_revm_latest/log4_32b"].status == "control-ok"
    assert by_key["group/revm_pinned/case"].status == "control-ok"
    assert by_key["system_contract_100x/rex5/case"].status == "untouched"
    # Counters exclude controls: only the one improved subject
    assert any("improved=1" in n for n in r_a.notes), r_a.notes
    assert any("regressed=0" in n for n in r_a.notes), r_a.notes
    assert any("control rows:" in n for n in r_a.notes)
    assert any("exceeded=0" in n for n in r_a.notes)
    print("#47a OK: subject improved + control-ok → CONFIRMED")

    # (b) control beyond bound → CONTROL_ANOMALY even when subjects all improved
    r_b = _tm.judge_terminal(
        _doc({
            "log_opcodes/rex4/log4_32b": 10000,
            "log_opcodes/op_revm_latest/log4_32b": 20000,
        }),
        _doc({
            "log_opcodes/rex4/log4_32b": 9000,        # -10% subject improved
            "log_opcodes/op_revm_latest/log4_32b": 21000,  # +5% > 2% bound
        }),
        epsilon_pct=0.1,
        control_lanes=CONTROL,
        control_composition_bound_pct=BOUND,
    )
    assert r_b.verdict == _tm.TERMINAL_CONTROL_ANOMALY, r_b
    by_b = {rd.row_key: rd for rd in r_b.rows}
    assert by_b["log_opcodes/rex4/log4_32b"].status == "improved"
    assert by_b["log_opcodes/op_revm_latest/log4_32b"].status == "control-anomaly"
    assert any("exceeded=1" in n for n in r_b.notes)
    print("#47b OK: control-anomaly → TERMINAL_CONTROL_ANOMALY (fail-closed)")

    # (c) no control_lanes → byte-identical legacy verdict + row statuses
    base_rows = {
        "log_opcodes/op_revm_latest/log4_32b": 20000,
        "log_opcodes/rex4/log4_32b": 10000,
    }
    cand_rows = {
        "log_opcodes/op_revm_latest/log4_32b": 20100,  # +0.5% → regressed under 0.1
        "log_opcodes/rex4/log4_32b": 9000,             # -10% improved
    }
    r_legacy = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1)
    r_empty = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1,
        control_lanes=None)
    r_empty2 = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1,
        control_lanes=[])
    assert r_legacy.verdict == _tm.TERMINAL_MIXED, r_legacy
    assert r_empty.verdict == r_legacy.verdict
    assert r_empty2.verdict == r_legacy.verdict
    assert [rd.status for rd in r_legacy.rows] == [rd.status for rd in r_empty.rows]
    assert [rd.status for rd in r_legacy.rows] == [rd.status for rd in r_empty2.rows]
    assert [rd.delta_pct for rd in r_legacy.rows] == [
        rd.delta_pct for rd in r_empty.rows]
    # Same inputs WITH control_lanes flip the op_revm row out of subject counters
    r_lane = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1,
        control_lanes=CONTROL, control_composition_bound_pct=BOUND)
    assert r_lane.verdict == _tm.TERMINAL_CONFIRMED, r_lane  # only subject improved
    assert {rd.row_key: rd.status for rd in r_lane.rows}[
        "log_opcodes/op_revm_latest/log4_32b"] == "control-ok"
    print("#47c OK: absent control_lanes → byte-identical legacy verdict/statuses")

    # (e) re-judge round-trip: old MIXED (single-threshold) → CONFIRMED under lanes
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # Build a TerminalResult dict as if measured under legacy thresholds
        old = _tm.judge_terminal(
            _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1)
        assert old.verdict == _tm.TERMINAL_MIXED
        old.env_fingerprint = "codspeed=1;rustc=1.80"
        in_path = d / "terminal.json"
        in_text = json.dumps(old.to_dict(), ensure_ascii=False, indent=1) + "\n"
        in_path.write_text(in_text)

        rejudged = _tm.rejudge_terminal_doc(
            json.loads(in_path.read_text()),
            epsilon_pct=0.1,
            floors={},
            default_floor_pct=0.1,
            control_lanes=CONTROL,
            control_composition_bound_pct=BOUND,
        )
        assert rejudged.verdict == _tm.TERMINAL_CONFIRMED, rejudged
        assert rejudged.profile_fingerprint == "fp-lane"
        assert rejudged.env_fingerprint == "codspeed=1;rustc=1.80"
        assert rejudged.rounds == old.rounds
        assert any("re-judged offline" in n for n in rejudged.notes)
        assert any("control_lanes=" in n for n in rejudged.notes)

        # Simulate CLI write path: never overwrite input
        out_path = Path(str(in_path) + ".rejudged.json")
        out_path.write_text(
            json.dumps(rejudged.to_dict(), ensure_ascii=False, indent=1) + "\n")
        assert in_path.read_text() == in_text  # input unmodified
        assert out_path.is_file()
        loaded = json.loads(out_path.read_text())
        assert loaded["verdict"] == "TERMINAL_CONFIRMED"
        assert any("re-judged offline" in n for n in loaded["notes"])
    print("#47e OK: rejudge round-trip flips MIXED→CONFIRMED, input intact")

    # (f) apply_terminal / is_mergeable keep mergeable=false
    assert _mf.is_mergeable(
        "byte-identical", "pass",
        terminal=_tm.TERMINAL_CONTROL_ANOMALY,
        terminal_required=True) is False
    m = {
        "accepted": [{
            "id": "c0", "regime": "byte-identical", "critic_verdict": "pass",
            "mergeable": True,
        }],
    }
    m2 = _mf.apply_terminal(m, r_b, terminal_required=True)
    assert m2["terminal"]["verdict"] == _tm.TERMINAL_CONTROL_ANOMALY
    assert m2["accepted"][0]["terminal"] == _tm.TERMINAL_CONTROL_ANOMALY
    assert m2["accepted"][0]["mergeable"] is False

    # Spec resolvers: default bound when lanes declared; empty when absent
    sp_lanes = SimpleNamespace(
        control_lanes=CONTROL, control_composition_bound_pct=None, raw={})
    assert _tm.resolve_control_lanes(sp_lanes) == CONTROL
    assert _tm.resolve_control_composition_bound_pct(sp_lanes) == 2.0
    sp_none = SimpleNamespace(control_lanes=[], raw={})
    assert _tm.resolve_control_lanes(sp_none) == []
    sp_raw = SimpleNamespace(
        control_lanes=None, control_composition_bound_pct=None,
        raw={"control_lanes": ["revm_pinned"], "control_composition_bound_pct": 1.5})
    assert _tm.resolve_control_lanes(sp_raw) == ["revm_pinned"]
    assert _tm.resolve_control_composition_bound_pct(sp_raw) == 1.5
    print("#47f OK: apply_terminal mergeable=false + control resolvers")
    print("case 36 OK")


CASES = [case_01, case_02, case_03, case_04, case_05, case_06, case_07, case_08, case_09, case_11, case_12, case_14, case_15, case_16, case_17, case_18, case_19, case_20, case_21, case_22, case_23, case_24, case_25, case_26, case_27, case_28, case_29, case_30, case_31, case_32, case_33, case_34, case_35, case_36]


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
