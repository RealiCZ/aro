from __future__ import annotations

import json
import tempfile
from pathlib import Path

from aro.engine import run_backtest
from aro.events import EventLog
from aro.generator import PlannedGenerator
from aro.store import Memory
from aro.types import Edit, Metrics, Verdict
from aro.types import Edit as _E

from tests.common import FAST, MockTarget


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

def case_37():
    """T15: final operator checkpoint at run_finished — last-round accept not swallowed.

    Reproduces the swallow: a synthetic run whose LAST round accepts a large-delta
    candidate. Mid-run checkpoints ride on round_started (TOP of each round → prior
    results only), so without a final flush that candidate appears in no checkpoint.
    After the fix it appears exactly once (via run_finished), and a mid-attempt accept
    already flushed by a later round_started is not double-reported as a final.
    """
    print("=== case 37: final checkpoint emission (last-round swallow) ===")
    from aro import runlog as _rl

    # (a) LAST round accepts a large delta — the swallow shape.
    # r0: NoOp (within-noise); r1: FAST edit (~−5% each apply; large vs empty front).
    plan = [
        ("noise", "noop control first round", []),
        ("outlier", "last-round large delta win", [Edit(FAST, "x", "y")]),
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
        assert report.pareto == ["outlier-r1"], report.pareto
        ev = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]

    # Mid-run: no round_started after r1 → outlier-r1 never in a round_started summary.
    rs = [e for e in ev if e["event"] == "round_started"]
    assert len(rs) == 2, [e.get("round") for e in rs]
    assert not any("outlier-r1" in (e.get("memory_summary") or "") for e in rs), \
        "last-round accept must not appear on any round_started (emitted at TOP)"

    # Final: run_finished carries the checkpoint with the last-round accept.
    rf = [e for e in ev if e["event"] == "run_finished"]
    assert len(rf) == 1 and "memory_summary" in rf[0], rf
    assert "outlier-r1" in rf[0]["memory_summary"], rf[0]["memory_summary"]
    assert "accepted_so_far" in rf[0] and rf[0]["accepted_so_far"] >= 1, rf[0]

    # Consumer surface: exactly one checkpoint mentions the outlier.
    cps = _rl.operator_checkpoints(ev)
    mentions = [c for c in cps if "outlier-r1" in c]
    assert len(mentions) == 1, (len(mentions), cps)
    print("#48a OK: last-round accept appears exactly once in operator checkpoints")

    # (b) Mid-attempt accept already flushed by subsequent round_started → final
    # is still emitted only when memory moved past that snapshot (r1 NoOp adds a
    # within-noise row, so final differs and is kept). Dedup fires when the last
    # round adds nothing: rounds=1 empty plan after a pre-seeded accept is awkward;
    # instead prove identical consecutive finish summaries collapse, and that a
    # pure round_started-only stream (no final field) still surfaces mid-run accepts.
    mid_plan = [
        ("opt", "mid-run accept", [Edit(FAST, "x", "y")]),
        ("ctrl", "noop on advanced baseline", []),
    ]
    with tempfile.TemporaryDirectory() as d2:
        d2 = Path(d2)
        target2 = MockTarget()
        memory2 = Memory(d2)
        events2 = EventLog(d2 / "events.jsonl", also_console=False)
        report2 = run_backtest(target2, PlannedGenerator(mid_plan), memory2,
                               rounds=2, candidates_per_round=1,
                               aa_runs=2, ab_pairs=3, baseline_ref="HEAD",
                               events=events2)
        assert "opt-r0" in report2.pareto, report2.pareto
        ev2 = [json.loads(l) for l in (d2 / "events.jsonl").read_text().splitlines()]

    rs2 = [e for e in ev2 if e["event"] == "round_started"]
    # r1's round_started (TOP of round 1) already checkpoints opt-r0.
    assert any("opt-r0" in (e.get("memory_summary") or "") for e in rs2), rs2
    cps2 = _rl.operator_checkpoints(ev2)
    # opt-r0 may appear in mid-run + final (final also lists the r1 within-noise
    # row, so summaries differ — not an identical double-report). Count mid-run
    # mentions: at least one round_started has it.
    assert sum(1 for c in cps2 if "opt-r0" in c) >= 1, cps2
    print("#48b OK: mid-run accept still checkpointed via subsequent round_started")

    # (c) Dedup: when final summary equals the last round_started summary, the
    # engine omits memory_summary on run_finished (no silent double-report).
    # Simulate with a one-round empty propose after memory already matches —
    # rounds=1 with empty plan: only round_started (empty memory) then finish
    # with still-empty memory → summaries equal → no final memory_summary field.
    empty_plan = []  # PlannedGenerator returns [] every round
    with tempfile.TemporaryDirectory() as d3:
        d3 = Path(d3)
        events3 = EventLog(d3 / "events.jsonl", also_console=False)
        run_backtest(MockTarget(), PlannedGenerator(empty_plan), Memory(d3),
                     rounds=1, candidates_per_round=1,
                     aa_runs=2, ab_pairs=3, baseline_ref="HEAD",
                     events=events3)
        ev3 = [json.loads(l) for l in (d3 / "events.jsonl").read_text().splitlines()]
    rf3 = next(e for e in ev3 if e["event"] == "run_finished")
    rs3 = [e for e in ev3 if e["event"] == "round_started"]
    assert len(rs3) == 1 and "memory_summary" in rs3[0]
    # empty memory throughout → final equals last checkpoint → field omitted
    assert "memory_summary" not in rf3, rf3
    cps3 = _rl.operator_checkpoints(ev3)
    assert len(cps3) == 1, cps3  # only the round_started empty-memory checkpoint
    print("#48c OK: final checkpoint omitted when identical to last round_started")

    # (d) attempt_finished carries the same additive fields (event shape contract).
    # Hermetic: emit through a real EventLog and parse back.
    with tempfile.TemporaryDirectory() as d4:
        d4 = Path(d4)
        elog = EventLog(d4 / "events.jsonl", also_console=False)
        mem4 = Memory(d4 / "a1")
        from aro.types import Candidate as _C, Patch as _PP, EvalOutcome as _EO
        mem4.record(_C(id="win-r0", hypothesis="h",
                       patch=_PP([Edit(FAST, "x", "y")])),
                    _EO("win-r0", Verdict.ACCEPTED))
        elog.emit("attempt_finished", fn="hot", verdict="accepted",
                  delta=-19.15, accepted=True, regime="byte-identical",
                  memory_summary=mem4.summary(),
                  accepted_so_far=len(mem4.accepted_edits()))
        ev4 = [json.loads(l) for l in (d4 / "events.jsonl").read_text().splitlines()]
    af = ev4[-1]
    assert af["event"] == "attempt_finished"
    assert "win-r0" in af["memory_summary"] and af["accepted_so_far"] == 1
    assert any("win-r0" in c for c in _rl.operator_checkpoints(ev4))
    print("#48d OK: attempt_finished accepts memory_summary checkpoint fields")
    print("case 37 OK")


def case_45():
    """T30-A: resume re-apply — acceptance order, degraded prefix, total-fail naming."""
    print("=== case 45: resume re-apply (degraded / total-fail / happy) ===")
    from aro.types import Candidate as _C, Patch as _P, Edit as _E, EvalOutcome as _EO

    class _ResumeTgt:
        """In-memory files with real SEARCH/REPLACE so a corrupted mid-chain
        edit fails while the prefix stays applied."""
        name = "resume-tgt"

        def __init__(self, init=None):
            self._init = init or {"a.rs": "A0", "b.rs": "B0", "c.rs": "C0"}
            self._wt, self._tick = {}, 0
            self.apply_log = []

        def objectives(self):
            return []

        def make_worktree(self, tag):
            self._tick += 1
            p = f"rt-{tag}-{self._tick}"
            self._wt[p] = dict(self._init)
            return p

        def remove_worktree(self, w):
            self._wt.pop(w, None)

        def apply(self, patch, work):
            f = self._wt[work]
            for e in patch.edits:
                c = f.get(e.path, "")
                if c.count(e.search) != 1:
                    raise RuntimeError(f"search text not found in {e.path}")
                i = c.find(e.search)
                f[e.path] = c[:i] + e.replace + c[i + len(e.search):]
                self.apply_log.append((work, e.path, e.search, e.replace))

        def build(self, work):
            pass

        def test(self, work):
            pass

        def differential(self, work, baseline):
            return True

        def bench(self, work, scale=1):
            m = Metrics()
            m.put("metric/x", [100.0, 100.0, 100.0])
            return m

    def _seed(d, edits_by_id):
        """Write pareto + patches for accepted candidates (acceptance order)."""
        mem = Memory(Path(d))
        for cid, edits in edits_by_id:
            mem.record(
                _C(id=cid, hypothesis=cid,
                   patch=_P([_E(p, s, r) for p, s, r in edits])),
                _EO(cid, Verdict.ACCEPTED))
        return mem

    # --- (a) three-edit resume, edit 2 SEARCH corrupted → resume_degraded ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # Record three accepts; second edit's SEARCH will not match baseline.
        _seed(d, [
            ("e1", [("a.rs", "A0", "A1")]),
            ("e2", [("b.rs", "B_WRONG", "B1")]),   # corrupted SEARCH
            ("e3", [("c.rs", "C0", "C1")]),
        ])
        tgt = _ResumeTgt()
        ev = EventLog(d / "events.jsonl", also_console=False)
        # Empty generator → rounds produce no candidates; resume still runs.
        rep = run_backtest(tgt, PlannedGenerator([]), Memory(d),
                           rounds=1, candidates_per_round=1,
                           aa_runs=2, ab_pairs=2, baseline_ref="HEAD",
                           events=ev)
        events = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()
                  if l.strip()]
        deg = [e for e in events if e["event"] == "resume_degraded"]
        assert len(deg) == 1, deg
        assert deg[0]["failed_candidate"] == "e2", deg[0]
        assert deg[0]["failed_file"] == "b.rs", deg[0]
        assert deg[0]["applied"] == 1, deg[0]
        assert deg[0]["total"] == 3, deg[0]
        # Prefix (edit 1) applied once; edit 2 never applied (failed SEARCH).
        applied_paths = [p for _, p, _, _ in tgt.apply_log]
        assert applied_paths.count("a.rs") >= 1, tgt.apply_log
        assert "b.rs" not in applied_paths, tgt.apply_log
        assert "c.rs" not in applied_paths, tgt.apply_log
        assert len(rep.folded_edits) == 0  # no new accepts this run
        # run continued (run_finished present) — not aborted by resume
        assert any(e["event"] == "run_finished" for e in events), events[-3:]
        print("#45a OK: resume_degraded names edit 2 (e2/b.rs), prefix applied, run continues")

    # --- (b) all-fail → legacy hard error naming the failing edit ---
    with tempfile.TemporaryDirectory() as d2:
        d2 = Path(d2)
        _seed(d2, [
            ("bad1", [("a.rs", "NOPE", "A1")]),
            ("e2", [("b.rs", "B0", "B1")]),
        ])
        tgt2 = _ResumeTgt()
        ev2 = EventLog(d2 / "events.jsonl", also_console=False)
        try:
            run_backtest(tgt2, PlannedGenerator([]), Memory(d2),
                         rounds=1, candidates_per_round=1,
                         aa_runs=2, ab_pairs=2, baseline_ref="HEAD",
                         events=ev2)
            raise AssertionError("expected resume total-failure RuntimeError")
        except RuntimeError as exc:
            msg = str(exc)
            assert "resume failed" in msg, msg
            assert "candidate=bad1" in msg, msg
            assert "file=a.rs" in msg, msg
            assert "after 0 clean apply" in msg, msg
        print("#45b OK: all-fail raises with failing edit (candidate + file)")

    # --- (c) happy path — all three apply; baseline_resumed; no resume_degraded ---
    with tempfile.TemporaryDirectory() as d3:
        d3 = Path(d3)
        _seed(d3, [
            ("e1", [("a.rs", "A0", "A1")]),
            ("e2", [("b.rs", "B0", "B1")]),
            ("e3", [("c.rs", "C0", "C1")]),
        ])
        tgt3 = _ResumeTgt()
        ev3 = EventLog(d3 / "events.jsonl", also_console=False)
        run_backtest(tgt3, PlannedGenerator([]), Memory(d3),
                     rounds=1, candidates_per_round=1,
                     aa_runs=2, ab_pairs=2, baseline_ref="HEAD",
                     events=ev3)
        events3 = [json.loads(l) for l in (d3 / "events.jsonl").read_text().splitlines()
                   if l.strip()]
        assert not any(e["event"] == "resume_degraded" for e in events3), events3
        resumed = [e for e in events3 if e["event"] == "baseline_resumed"]
        assert len(resumed) == 1 and resumed[0]["edits"] == 3, resumed
        # All three files applied at least once during resume.
        applied3 = {p for _, p, _, _ in tgt3.apply_log}
        assert {"a.rs", "b.rs", "c.rs"} <= applied3, tgt3.apply_log
        print("#45c OK: happy-path resume applies all 3, baseline_resumed, no degrade")

    # Ordering source: accepted_edit_chain follows pareto append order.
    with tempfile.TemporaryDirectory() as d4:
        d4 = Path(d4)
        mem = _seed(d4, [
            ("c-first", [("f.rs", "X", "Y")]),
            ("c-second", [("g.rs", "P", "Q")]),
        ])
        chain = mem.accepted_edit_chain()
        assert [cid for cid, _ in chain] == ["c-first", "c-second"], chain
        assert [e.path for _, e in chain] == ["f.rs", "g.rs"], chain
        assert mem.accepted_edits()[0].path == "f.rs"
    print("#45d OK: accepted_edit_chain order = pareto acceptance sequence")
    print("case 45 OK")

