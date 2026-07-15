"""attempt — the L3 unattended meta-loop (walk the frontier, judge, compound).

Extracted from sweep.py: the per-function attempt driver, the L4a probe rescue,
the parent non-regression gate, and the end-of-run finalize (decision tree +
manifest + charts). The L1 frontier MAP (report-only) stays in sweep.py.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Optional

from . import eval as evalmod
from . import permtree
from . import lessons as lessonsmod
from .frontier import (_explore_decision, _floor_pct, _lesson_index,
                       _locate_fn, _pending_names, _promote_pending,
                       _refill_queue, _split_headroom, _workspace_tokens,
                       bucket_functions)
from .llm import select_backend
from .report_md import render_explore_report
from .target import SpecTarget
from .types import Patch, best_improvement

# --- L3: --attempt — the unattended meta-loop ---------------------------------
#
# The map (above) is L1: report-only, no changes. `aro run` is L2: propose one
# change, a human reviews/merges. `--attempt` is L3: unattended — it walks the
# actionable frontier heaviest-first, runs the FULL per-target loop (the same
# deterministic judge: A/A floor + paired A/B + differential + auto-tighten) on
# each hot function, folds an accepted patch into the shared baseline, and
# re-profiles on top of it (compounding) until the frontier is exhausted or the
# attempt budget runs out. It writes NO new judging code — it orchestrates the
# existing `run_backtest` + `profile_ranked`.
#
# Loop-ready by construction (the four primitives a self-running loop needs):
#   budget   — `--max-attempts` caps the fan-out; `bench_scales` bounds re-benching.
#   run-log  — every attempt + every candidate verdict streams to events.jsonl.
#   gate     — architecture-gated functions are surfaced, never auto-touched; an
#              `accepted` patch is correctness+speed proven, NOT "should-merge".
#   denylist — the per-function region guard locks edits to the located source file;
#              Cargo.toml/lock, benches/, tests/ stay off-limits (the judge's rule).
#
# Comprehension debt: N unattended accepts leave N diffs a human still has to
# understand before merging. The attempt map lists exactly those diffs so the debt
# is visible, not hidden — review them; `accepted` ≠ merged.

# Verdict informativeness, best first — for picking the headline verdict of a
# per-function run from its candidates (accept is detected separately, from the
# shared pareto growing, since pareto is cumulative across functions).
_VERDICT_RANK = {"accepted": 6, "accepted-ir": 6, "noise-limited": 5,
                 "regressed": 4, "regressed-ir": 4,
                 "within-noise": 3, "neutral-ir": 3,
                 "verify-failed": 2, "no-coverage": 2,
                 "build-failed": 1, "rejected": 0,
                 # defensive completeness: retroactive/backfill verdict live per-run consumers normally never see
                 "refuted-by-icount": 0,
                 # pre-PR criterion Ir gate (not attempt headlines; classification completeness)
                 "TERMINAL_CONFIRMED": 5,
                 "TERMINAL_UNTOUCHED": 0,
                 "TERMINAL_REGRESSED": 0,
                 "TERMINAL_MIXED": 0,
                 "TERMINAL_TEST_FAILED": 0,
                 "TERMINAL_CONTROL_ANOMALY": 0}

# Critic rubric stems that constitute a genuine ARCHITECTURE/scope objection —
# the only findings that gate a function (future wins route to the relaxed,
# never-auto-merged regime). Cheating (reward-hack) and behaviour-suspect
# findings condemn the CANDIDATE, not the function, and must not gate it.
_GATING_RUBRICS = ("layer-dissolve", "conflate", "discoverab", "scope-limit")

# Consecutive zero-candidate attempts (report.outcomes empty, on DISTINCT fns)
# before the run declares the generation agent hard-down and aborts. One dry
# fn happens; three in a row is infrastructure (quota / auth / dead CLI), and
# every further attempt burns wall-clock writing `no-candidate` non-judgments
# into the ledger — rex5-01 walked its whole frontier this way while claude
# was quota-dead, then closed with a dishonest "headroom drained" claim.
_GENERATOR_DOWN_AFTER = 3
_GENERATOR_DOWN = "generator hard-down"


def _generator_down(headline_verdicts) -> bool:
    """True when the last _GENERATOR_DOWN_AFTER headline verdicts are ALL
    no-candidate — zero candidates reached the judge across several distinct
    functions in a row."""
    k = _GENERATOR_DOWN_AFTER
    return (len(headline_verdicts) >= k
            and all(v == "no-candidate" for v in headline_verdicts[-k:]))


def _lesson_gated(outcome) -> bool:
    """Structured gating decision at lesson WRITE time: True only when the critic
    REJECTED this candidate on an architectural rubric. Written explicitly into
    the lesson row so the read side never keyword-sniffs new rows."""
    if outcome.verdict.value != "rejected":
        return False
    return any(any(s in (ru or "").lower() for s in _GATING_RUBRICS)
               for ru in getattr(outcome, "critic_rubrics", []))



def _summarize_report(report, minz: dict):
    """(headline_verdict, best_delta_pct) for one per-function run, from its OWN
    candidates (report.outcomes is per-call; report.pareto is shared/cumulative).
    Direction-aware: best Δ is the largest improvement in each metric's own direction."""
    if not report.outcomes:
        return "no-candidate", None

    best_v, best_d = None, None
    for _cand, o in report.outcomes:
        v = o.verdict.value
        if best_v is None or _VERDICT_RANK.get(v, 0) > _VERDICT_RANK.get(best_v, 0):
            best_v = v
            b = best_improvement(o.deltas, minz)
            best_d = b[0].delta_pct if b else None
    return best_v, best_d


def _seed_memory(mem_dir, cumulative_edits):
    """A FRESH per-attempt Memory pre-seeded with the cumulative accepted patch under
    UNIQUE ids (`base-0`, `base-1`, …), so run_backtest's resume re-applies the wins so
    far (correct compounding) without the live agent's reused candidate id colliding."""
    from .store import Memory
    from .types import Candidate, EvalOutcome, Patch, Verdict
    m = Memory(mem_dir)
    for j, e in enumerate(cumulative_edits):
        cid = f"base-{j}"
        m.record(Candidate(id=cid, hypothesis="", patch=Patch([e])),
                 EvalOutcome(cid, Verdict.ACCEPTED, [], []))
    return m



def _archive_rejected(out_dir: Path, rels, events, *, reason: str) -> None:
    """A probe that failed qualification (or whose author died) moves out of the
    checkout's probes/ into the run's out-dir under rejected-probes/ — the repo
    stays clean, the artifact stays auditable next to the events that rejected
    it (sha + reasons are already in the log). Archive, never plain-delete."""
    import shutil
    from .workload_factory import REPO_ROOT
    dest = Path(out_dir) / "rejected-probes"
    for rel in rels:
        src = REPO_ROOT / rel
        if not src.exists():
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / Path(rel).name))
            events.emit("probe_archived", probe=rel,
                        to=str(dest / Path(rel).name), reason=reason[:160])
        except Exception as e:
            events.emit("probe_archive_failed", probe=rel, detail=str(e)[:160])


def _parent_nonregression(parent_spec, base_edits: list, new_edits: list,
                          floors, minz: dict, events, fn: str) -> bool:
    """A micro-proven win must not regress the PARENT workload before it folds:
    paired A/B on the parent bench — base (cumulative wins) vs base+new — judged
    against the parent's own A/A floors. True = safe to fold. Failure of the
    machinery itself returns False (never fold on an unverified claim)."""
    t = SpecTarget(parent_spec)
    base_w = cand_w = None
    try:
        base_w = t.make_worktree("parentchk-base")
        cand_w = t.make_worktree("parentchk-cand")
        t.apply(Patch(edits=list(base_edits)), base_w)
        t.apply(Patch(edits=list(base_edits)), cand_w)
        t.apply(Patch(edits=list(new_edits)), cand_w)
        t.build(base_w)
        t.build(cand_w)
        objs = t.objectives()
        obj_min = {o.metric: o.minimize for o in objs}
        deltas, agg = evalmod._significance(
            t, base_w, cand_w, parent_spec.ab_pairs, 1, obj_min, objs, floors)
        events.emit("parent_check", fn=fn, regressed=agg["regressed"],
                    deltas=[{"metric": d.metric, "delta_pct": round(d.delta_pct, 3)}
                            for d in deltas])
        goal = parent_spec.bench.get("metric")
        pd = next((d.delta_pct for d in deltas if d.metric == goal),
                  deltas[0].delta_pct if deltas else None)
        return (not agg["regressed"], pd)
    except Exception as e:
        events.emit("parent_check", fn=fn, regressed=None, error=str(e)[:200])
        return (False, None)
    finally:
        for w in (base_w, cand_w):
            if w is not None:
                t.remove_worktree(w)


def _probe_rescue(spec, derived, fn: str, files: list, pct: float, parent_floors,
                  minz: dict, cumulative_edits: list, out_dir: Path, ran: int,
                  events, *, fanout: int, gen_concurrency: int, rounds_per_fn: int,
                  prescreen: bool, critic, per_fn_dry: int, hooks: dict,
                  regime: str = "micro-proven", ledger_name: str = None,
                  backend=None):
    """L4a orchestration for ONE noise-limited node: author → qualify (frozen) →
    re-judge under the micro-bench (Gate 1 stays the PARENT oracle) → parent
    non-regression → fold. Returns (ran, row|None, new_edits). `hooks` injects
    author/bench/profile_shares/rejudge/parent_check for tests; production uses
    the real backends."""
    from . import probe_factory as pfmod
    from .engine import run_backtest
    from .generator import AgenticGenerator, RalphGenerator

    # 0) fragile-assumption check: the PARENT differential must actually constrain
    # this fn (seeded mutation must alarm). False -> weak-oracle node, no rescue —
    # a micro-bench win we couldn't correctness-guarantee would be a false claim.
    covers = hooks.get("parent_covers", pfmod.parent_differential_covers)
    covered = covers(derived, fn, files, events=events)
    if covered is False:
        return ran, None, []

    # 1) author (a separate agent call; never sees any candidate patch)
    try:
        author = hooks.get("author") or pfmod.author
        probe_rel = author(derived, fn, files)
    except Exception as e:
        events.emit("probe_author_failed", fn=fn, detail=str(e)[:200])
        # whatever half-written probe the dead author left behind
        _archive_rejected(out_dir, [pfmod.probe_rel_path(derived.name, fn)],
                          events, reason=f"author failed: {str(e)[:80]}")
        return ran, None, []

    # 2) qualification gates + freeze (probe_registered)
    q = pfmod.qualify(derived, fn, probe_rel,
                      parent_floors=parent_floors, objectives=SpecTarget(derived).objectives(),
                      aa_runs=spec.aa_runs, bench=hooks.get("bench"),
                      profile_shares=hooks.get("profile_shares"), events=events)
    if not q.ok:
        _archive_rejected(out_dir, [probe_rel], events,
                          reason="micro probe failed qualification")
        return ran, None, []

    # 3) re-judge as its OWN attempt row, regime micro-proven
    micro = pfmod.micro_spec(derived, fn, probe_rel)
    backend = backend or select_backend(micro)
    ran += 1
    events.context = {"attempt": ran}
    events.emit("attempt_started", fn=fn, pct=round(pct, 2), try_n=1,
                regime=regime, files=files, probe=q.sha256[:12])
    rejudge = hooks.get("rejudge")
    amem = None  # only populated on the real backtest path (not the test rejudge hook)
    if rejudge is not None:
        report = rejudge(micro, ran)
    else:
        dtarget = SpecTarget(micro)
        generator = (RalphGenerator(dtarget, gen_concurrency=gen_concurrency,
                                    backend=backend)
                     if spec.generator == "ralph"
                     else AgenticGenerator(dtarget, gen_concurrency=gen_concurrency,
                                           backend=backend))
        amem = _seed_memory(out_dir / f"a{ran}", cumulative_edits)
        try:
            report = run_backtest(
                dtarget, generator, amem,
                rounds=rounds_per_fn, candidates_per_round=fanout,
                aa_runs=spec.aa_runs, ab_pairs=spec.ab_pairs,
                baseline_ref=spec.baseline_ref, events=events, goal=spec.goal,
                stop_dry_rounds=per_fn_dry, read_phase=spec.read_phase,
                bench_scales=spec.bench_scales, prescreen=prescreen, critic=critic,
                critic_context=(f"Target function `{fn}` re-judged under a QUALIFIED "
                                f"isolation micro-bench (sha {q.sha256[:12]}). Judge "
                                f"reward-hacking as usual; the probe itself is frozen."))
        except Exception as e:
            events.emit("attempt_errored", fn=fn, detail=str(e)[:200])
            return ran, {"name": fn, "pct": pct, "verdict": "errored", "delta": None,
                         "files": files, "regime": regime}, []
    verdict, delta = _summarize_report(report, minz)

    # 4) parent non-regression before the fold (correctness is already parent-proven:
    #    the micro spec keeps the parent differential + test suite as Gate 1)
    new_edits: list = []
    parent_delta = None
    if report.folded_edits:
        check = hooks.get("parent_check") or _parent_nonregression
        res = check(derived, cumulative_edits, report.folded_edits, parent_floors,
                    minz, events, fn)
        ok, parent_delta = res if isinstance(res, tuple) else (bool(res), None)
        if ok:
            new_edits = list(report.folded_edits)
        else:
            verdict = "parent-regressed"
    # delta = the MICRO-bench Δ (the proven claim, at micro power); parent_delta =
    # the parent-workload point estimate — the only number that may compound into
    # whole-workload realized (compounding the micro Δ would overstate it by the
    # fn's share of runtime — dishonest by an order of magnitude).
    row = {"name": fn, "pct": pct, "verdict": verdict, "delta": delta,
           "parent_delta": (round(parent_delta, 3) if isinstance(parent_delta, (int, float))
                            else None),
           "files": files, "accepted": bool(new_edits), "regime": regime,
           "probe": q.sha256[:12]}
    # Final operator checkpoint (parity with run_finished): last-round accepts
    # otherwise vanish — no subsequent round_started flushes them. Same payload
    # shape as mid-run round_started. Skipped when the test rejudge hook bypasses
    # the real Memory (amem is None).
    fin = dict(fn=fn, verdict=verdict,
               delta=(round(delta, 3) if isinstance(delta, (int, float)) else None),
               accepted=bool(new_edits), regime=regime)
    if amem is not None:
        fin["memory_summary"] = amem.summary()
        fin["accepted_so_far"] = len(amem.accepted_edits())
    events.emit("attempt_finished", **fin)
    return ran, row, new_edits


def _record_residue(ledger_name: str, spec, buckets, tries: dict,
                    cumulative_edits: list, out_dir: Path, events,
                    reason: str) -> int:
    """Untried residue → the ledger. The permanent tree only knows JUDGED
    nodes; whatever the frontier saw but this run never attempted would
    evaporate at stop, letting the union view read "complete" while hot fns
    were never tried. Record the leftovers — but ONLY fns with no ledger
    record at all for this workload, so a prior verdict (especially an OPEN
    noise-limited case) is never shadowed by a residue row. Returns the count."""
    seen = {(r.get("workload"), r.get("fn")) for r in permtree.load(ledger_name)}
    base_state = permtree.baseline_state(cumulative_edits)
    n = 0
    for key, verdict in (("untried", "no-attempt"), ("tried", "no-attempt"),
                         ("gated", "gated")):
        for r in buckets.get(key, []):
            nm = r.get("name")
            if not nm or nm in tries or (spec.name, nm) in seen:
                continue
            hyp = (f"gated by lesson: {r.get('verdict', '')}" if verdict == "gated"
                   else f"frontier residue at stop: {reason}")
            permtree.record(ledger_name, workload=spec.name, fn=nm,
                            base_state=base_state, verdict=verdict,
                            regime="unattempted", pct=r.get("pct"),
                            hypothesis=hyp, events_ref=str(out_dir),
                            run_id=getattr(events, "run_id", ""))
            seen.add((spec.name, nm))
            n += 1
    if n:
        events.emit("frontier_residue", recorded=n, reason=reason[:160])
    return n


def attempt(spec, *, max_attempts: int, rounds_per_fn: int, min_pct: float,
            top: int, out_dir: Path, events, diverge: bool = False,
            max_tries_per_fn: int = 0, fanout: int = 1, gen_concurrency: int = 8,
            exhaustive: bool = False, prescreen: bool = False,
            per_fn_dry_rounds: int = 0, critic=None,
            probe_factory: bool = False, probe_hooks: dict = None,
            workload_regime: str = None, ledger_name: str = None) -> tuple:
    """The L3 meta-loop. Returns `(rows, memory)` where rows are the per-function
    attempt records (for the map) and memory is the shared store carrying the
    cumulative accepted patch.

    `diverge=False` is CONVERGENT: walk the untried frontier once, stop when it
    empties (the map is the product). `diverge=True` is the INFINITE/divergent
    autoresearch policy: never stop on dry — refill from tried/gated (escalation),
    re-attempt each function up to `max_tries_per_fn`, and run until the attempt
    BUDGET (`max_attempts`) is spent. Each attempt is tagged with its oracle REGIME
    (byte-identical, or `relaxed` for an architecture-gated target where a win is
    should-not-merge) so the trajectory can draw the two kinds of win differently.

    Infinite-flow (token-infinite) knobs — design §4.1/4.2/4.3b/4.4:
      `fanout`          — candidates generated PER ROUND, in parallel, each with a
                          different lens/framing (the agent-pool fan-out). >1 turns on
                          the parallel generator; 1 keeps the legacy single-candidate.
      `gen_concurrency` — cap on concurrent LLM generators (generation is
                          parallel; the JUDGE stays serial — that invariant is the moat).
      `prescreen`       — cheap build+smoke gate + dedup + priority order BEFORE the
                          serial judge, so junk candidates don't hog the scarce A/A+A/B.
      `exhaustive`      — drop the cost-saving cross-fn dry-stop; walk the whole tree.
      `per_fn_dry_rounds` — per-function dry-round cap (how many reflect rounds with no
                          accept before the function is judged exhausted); 0 → spec default."""
    # All observations — base and synthetic workloads alike — land in ONE permanent
    # ledger (the base spec's), distinguished by the `workload` field; closure()
    # must see every workload's open cases (review finding: split files hid them).
    ledger_name = ledger_name or spec.name
    backend = select_backend(spec)
    from .engine import run_backtest
    from .generator import AgenticGenerator, RalphGenerator

    target0 = SpecTarget(spec)
    our_token = _workspace_tokens(target0, spec.bench.get("pkg", spec.name))
    minz = {o["metric"]: o.get("minimize", True) for o in spec.objectives}
    # Driver-maintained cumulative patch — NOT a single shared Memory. The live agent
    # reuses one candidate id ("agent-r0") every attempt, which collides in a shared
    # store (the pareto SET dedups, and patches/<id>.txt gets overwritten), corrupting
    # both accept-detection and cross-attempt compounding. So each attempt gets a FRESH
    # memory seeded with `cumulative_edits` under unique ids, and an accept is detected
    # from that attempt's OWN report (not a pareto diff).
    cumulative_edits: list = []

    def reprofile():
        from .sweep import profile_ranked   # lazy: sweep imports attempt in main()
        ranked = profile_ranked(spec, top=top, our_token=our_token,
                                extra_edits=list(cumulative_edits))
        return bucket_functions(ranked, our_token, _lesson_index(spec.name), min_pct,
                                classify=spec.classify)

    buckets = reprofile()
    cap = max_tries_per_fn if max_tries_per_fn else (2 if diverge else 1)
    # Pending-first: the ledger's open debts for this workload (noise-limited
    # cases, never-tried residue) are re-attempted BEFORE fresh frontier — a
    # resumed campaign pays what it owes before exploring.
    pending = _pending_names(permtree.load(ledger_name), spec.name)
    if pending:
        queue = _promote_pending(buckets, pending, {}, cap)
        owed = [r["name"] for r in queue if r["name"] in pending]
        if owed:
            events.emit("pending_first", count=len(owed), fns=owed[:20])
    else:
        queue = list(buckets["untried"])
    events.emit("attempt_frontier", untried=len(queue), policy=("diverge" if diverge
                else "converge"), budget=max_attempts, cap=cap,
                fns=[r["name"] for r in queue[:max_attempts]])
    # Untouchable floor breakdown (for the report's clickable floor view): the not-ours
    # frames (crypto / runtime) with owner + why, heaviest first.
    events.emit("profile_floor", frames=[
        {"name": r["name"], "pct": round(r["pct"], 2), "owner": r["owner"], "why": r["why"]}
        for r in buckets.get("not_ours", [])[:40]])

    tries: dict = {}
    rows: list = []
    headline_verdicts: list = []   # raw per-attempt headlines (generator-down watch)
    ran = 0
    # explorer bookkeeping (diverge): compounded realized speedup, the set already
    # attempted (drives the shrinking headroom), the non-accept streak, and the
    # per-step log the running report + chart read.
    factor = 1.0
    attempted_names: set = set()
    dry_streak = 0
    elog: list = []
    floor_now = _floor_pct(buckets)
    stop_reason = f"attempt budget spent ({max_attempts})"
    _loc_cache: dict = {}

    def _loc(nm, sym=""):
        if nm not in _loc_cache:
            _loc_cache[nm] = bool(_locate_fn(target0, spec.bench["pkg"], nm, symbol=sym))
        return _loc_cache[nm]

    while ran < max_attempts:
        events.context = {}   # cleared between attempts; set to {"attempt": ran} below
        if not queue:
            queue = _refill_queue(buckets, tries, cap) if diverge else []
            if not queue:
                # CONVERGENT stops here (the frontier is a map); DIVERGENT only
                # reaches here when even the escalation is dry — truly nothing left.
                events.emit("attempt_exhausted", policy=("diverge" if diverge
                            else "converge"), ran=ran)
                stop_reason = "frontier exhausted"
                break

        F = queue.pop(0)
        name = F["name"]
        if tries.get(name, 0) >= cap:
            continue
        gated_names = {r["name"] for r in buckets.get("gated", [])}
        # Provenance (design W2): a synthetic workload's wins are never
        # byte-identical-mergeable — the workload's representativeness is a human call.
        regime = workload_regime or ("relaxed" if name in gated_names
                                     else "byte-identical")

        files = _locate_fn(target0, spec.bench["pkg"], name, symbol=F.get("symbol", ""))
        if not files:
            tries[name] = cap  # never retry an unlocatable name
            rows.append({"name": name, "pct": F["pct"], "verdict": "unlocated",
                         "delta": None, "files": [], "regime": regime})
            events.emit("attempt_skipped", fn=name, reason="source not located")
            permtree.record(ledger_name, workload=spec.name, fn=name,
                            base_state=permtree.baseline_state(cumulative_edits),
                            verdict="unlocated", regime=regime, pct=F["pct"],
                            events_ref=str(out_dir),
                            run_id=getattr(events, "run_id", ""))
            continue

        tries[name] = tries.get(name, 0) + 1
        attempted_names.add(name)
        ran += 1
        base_state = permtree.baseline_state(cumulative_edits)
        # Stamp every event from here (attempt_started, all backtest events, the win's
        # baseline_advanced, attempt_finished) with this attempt's a<N> index, so the
        # manifest/any consumer maps an event → its attempt dir without timeline-counting.
        events.context = {"attempt": ran}
        # Retarget the WHOLE task to this function, not just the editable regions:
        # the spec's `constraints.notes` (and the original hot_path framing) would
        # otherwise steer the agent at the spec's first function and the guard then
        # rejects the out-of-region edit. Override notes + editable to name `name`.
        per_fn_constraints = dict(spec.constraints)
        per_fn_constraints["editable"] = files
        per_fn_constraints["notes"] = (
            f"Optimize the hot function `{name}` (in {files[0]}). Edit ONLY the "
            f"listed file(s) and keep behaviour byte-identical. Do NOT optimize any "
            f"other function — this attempt targets `{name}` specifically.")
        derived = dataclasses.replace(
            spec, regions=files,
            context={"file": files[0], "anchors": [["fn", name]]},
            constraints=per_fn_constraints)
        dtarget = SpecTarget(derived)
        generator = (RalphGenerator(dtarget, gen_concurrency=gen_concurrency,
                                    backend=backend)
                     if spec.generator == "ralph"
                     else AgenticGenerator(dtarget, gen_concurrency=gen_concurrency,
                                           backend=backend))

        events.emit("attempt_started", fn=name, pct=round(F["pct"], 2),
                    try_n=tries[name], regime=regime, files=files)
        amem = _seed_memory(out_dir / f"a{ran}", cumulative_edits)  # fresh, no id collision
        try:
            report = run_backtest(
                dtarget, generator, amem,
                rounds=rounds_per_fn, candidates_per_round=fanout,
                aa_runs=spec.aa_runs, ab_pairs=spec.ab_pairs,
                baseline_ref=spec.baseline_ref, events=events,
                goal=spec.goal,
                stop_dry_rounds=(per_fn_dry_rounds or spec.stop.dry_rounds),
                read_phase=spec.read_phase, bench_scales=spec.bench_scales,
                prescreen=prescreen, critic=critic,
                critic_context=(
                    f"Target function `{name}` (in {files[0]}); workload probe "
                    f"{spec.profile.get('example', spec.bench['example'])}. Implementation-source "
                    f"edits only, behaviour preserved. Judge whether this is a reward-hack, a "
                    f"gamed bench, or a known-bad pattern (e.g. PR#313 dissolving layering)."))
        except Exception as e:
            rows.append({"name": name, "pct": F["pct"], "verdict": "errored",
                         "delta": None, "files": files, "regime": regime})
            events.emit("attempt_errored", fn=name, detail=str(e)[:200])
            continue

        verdict, delta = _summarize_report(report, minz)
        # Durable cross-run lesson per candidate → a later sweep dedups this fn
        # (untried → tried) automatically, on top of the in-run try counter.
        for cand, o in report.outcomes:
            b = best_improvement(o.deltas, minz)
            lessonsmod.append(spec.name, cand.hypothesis, o.verdict.value,
                              b[0].delta_pct if b else None,
                              o.notes[-1] if o.notes else "",
                              gated=_lesson_gated(o),
                              ir_delta_pct=getattr(o, "ir_delta_pct", None),
                              profile_fingerprint=getattr(o, "profile_fingerprint", None),
                              env_fingerprint=getattr(o, "env_fingerprint", None),
                              backend=backend.name)

        # The engine folded this attempt's round winners into its OWN baseline and reports
        # exactly those new edits as `folded_edits` (past the resumed seed). Adopt them —
        # never a per-outcome ACCEPTED that was superseded by a better sibling (it would
        # conflict on the next resume), never the seed twice. Empty on an early-errored run,
        # so a failed attempt leaves the driver's cumulative wins untouched.
        accepted_now = bool(report.folded_edits)
        cumulative_edits.extend(report.folded_edits)
        rows.append({"name": name, "pct": F["pct"], "verdict": verdict,
                     "delta": delta, "files": files, "accepted": accepted_now,
                     "regime": regime})
        # Final operator checkpoint — see _probe_rescue's attempt_finished note.
        events.emit("attempt_finished", fn=name, verdict=verdict,
                    delta=(round(delta, 3) if delta is not None else None),
                    accepted=accepted_now, regime=regime,
                    memory_summary=amem.summary(),
                    accepted_so_far=len(amem.accepted_edits()))
        best_hyp = next((c.hypothesis for c, o in report.outcomes
                         if o.verdict.value == verdict), "")
        # Surface Ir-gate fields from the headline outcome when present.
        head_o = next((o for c, o in report.outcomes if o.verdict.value == verdict), None)
        permtree.record(ledger_name, workload=spec.name, fn=name,
                        base_state=base_state, verdict=verdict, regime=regime,
                        delta=delta, pct=F["pct"], files=files, hypothesis=best_hyp,
                        events_ref=f"{out_dir}#a{ran}",
                        run_id=getattr(events, "run_id", ""),
                        ir_delta_pct=getattr(head_o, "ir_delta_pct", None) if head_o else None,
                        profile_fingerprint=(getattr(head_o, "profile_fingerprint", None)
                                             if head_o else None),
                        env_fingerprint=(getattr(head_o, "env_fingerprint", None)
                                         if head_o else None),
                        backend=backend.name)

        headline_verdicts.append(verdict)
        # Generation-agent hard-down: several consecutive attempts where ZERO
        # candidates reached the judge. Abort loudly instead of walking the
        # rest of the frontier into `no-candidate` non-judgments — the
        # untouched queue lands as `no-attempt` residue (still owed), and the
        # caller closes author-error so `aro next` routes retry-factory
        # instead of trusting this run's numbers.
        if _generator_down(headline_verdicts):
            stop_reason = (f"{_GENERATOR_DOWN}: {_GENERATOR_DOWN_AFTER} "
                           "consecutive zero-candidate attempts (see "
                           "generator_error events for the underlying failure)")
            events.emit("attempt_abort", reason=stop_reason)
            break

        # --- L4a: probe rescue — a noise-limited node gets an ISOLATION MICRO-BENCH
        # (authored + qualification-gated + frozen), a re-judge under it, and a
        # PARENT-workload non-regression check before its win may fold. Design
        # docs/self-extending-search-design.md §3.1; regime `micro-proven` is never
        # auto-mergeable (manifest keeps mergeable=false for non-byte-identical).
        if probe_factory and verdict == "noise-limited" and not accepted_now:
            ran, row2, new_edits = _probe_rescue(
                spec, derived, name, files, F["pct"], report.floors, minz,
                cumulative_edits, out_dir, ran, events,
                fanout=fanout, gen_concurrency=gen_concurrency,
                rounds_per_fn=rounds_per_fn, prescreen=prescreen, critic=critic,
                per_fn_dry=(per_fn_dry_rounds or spec.stop.dry_rounds),
                hooks=probe_hooks or {},
                # provenance: the MORE restrictive label wins — a synthetic
                # workload's rescue win must stay synthetic, never launder into
                # the trusted micro-proven bucket
                regime=(workload_regime or "micro-proven"),
                ledger_name=ledger_name, backend=backend)
            if row2 is not None:
                rows.append(row2)
                permtree.record(ledger_name, workload=spec.name, fn=name,
                                base_state=base_state, verdict=row2["verdict"],
                                regime="micro-proven", delta=row2.get("delta"),
                                parent_delta=row2.get("parent_delta"),
                                pct=F["pct"], files=files,
                                probe_sha=row2.get("probe"),
                                events_ref=f"{out_dir}#a{ran}",
                                run_id=getattr(events, "run_id", ""),
                                backend=backend.name)
                if new_edits:
                    cumulative_edits.extend(new_edits)
                    accepted_now = True
                    verdict = row2["verdict"]
                    regime = "micro-proven"          # the explorer log must not
                                                     # relabel a micro win byte-identical
                    delta = row2["parent_delta"]     # whole-workload compounding uses the
                                                     # PARENT point estimate, never the micro Δ

        if accepted_now:
            # The baseline moved → re-profile on top of all wins so far and re-bucket
            # (the ranking shifts; new functions may surface, dedup'd by the try cap).
            # Unpaid ledger debts stay at the front of the rebuilt queue.
            buckets = reprofile()
            queue = (_promote_pending(buckets, pending, tries, cap) if pending
                     else [r for r in buckets["untried"] if tries.get(r["name"], 0) < cap])
            events.emit("attempt_resweep", remaining=len(queue))

        # --- explorer step: headroom / realized / decision, then write report + chart ----
        if diverge:
            if accepted_now and isinstance(delta, (int, float)):
                factor *= (1 + delta / 100.0)
                dry_streak = 0
            else:
                dry_streak += 1
            realized_cum = (factor - 1) * 100.0          # negative = faster
            headroom, unreachable = _split_headroom(buckets, attempted_names, _loc)
            floor_now = _floor_pct(buckets)
            decision, reason = _explore_decision(headroom, dry_streak,
                                                 dry_max=(per_fn_dry_rounds or 3),
                                                 exhaustive=exhaustive)
            elog.append({"i": ran, "fn": name, "verdict": verdict, "delta": delta,
                         "accepted": accepted_now, "regime": regime,
                         "realized_cum": realized_cum, "headroom": headroom,
                         "unreachable": unreachable})
            events.emit("explore_step", i=ran, fn=name, verdict=verdict,
                        realized_pct=round(-realized_cum, 2),
                        headroom_pct=round(headroom, 2), unreachable_pct=round(unreachable, 2),
                        floor_pct=round(floor_now, 1), decision=decision, reason=reason)
            # running report + chart (overwritten each step — a live dashboard)
            try:
                profiled = spec.profile.get("example", spec.bench["example"])
                (out_dir / "REPORT.md").write_text(
                    render_explore_report(elog, spec.name, profiled, floor_now,
                                          decision, reason) + "\n")
                from . import chart as _chart
                (out_dir / "trajectory.svg").write_text(
                    _chart.explore_svg(elog, floor_now, decision, reason, spec.name) + "\n")
            except Exception as e:
                events.emit("explore_report_failed", detail=str(e)[:160])
            if decision == "STOP":
                events.emit("explore_stop", i=ran, reason=reason)
                stop_reason = reason
                break

    events.context = {}   # residue events are run-level, not the last attempt's
    _record_residue(ledger_name, spec, buckets, tries, cumulative_edits,
                    out_dir, events, stop_reason)
    return rows, cumulative_edits, stop_reason



def _finalize_run(out_dir: Path, events, *,
                  outlier_quarantine_pct: Optional[float] = None) -> None:
    """Closing step of an `--attempt` run (§4.5): from the verbatim events.jsonl,
    auto-build the interactive decision tree (`decision-tree.html`) and render the
    explorer's `trajectory.svg` to a `trajectory.png` (so a report can embed a PNG).
    All best-effort — a finalize failure never invalidates the run's truth (the
    events log is the source); it just means a derived artifact wasn't drawn.

    `outlier_quarantine_pct` is the target-spec tripwire (default 5.0 when omitted;
    explicit 0 disables). Pass the loaded TargetSpec's field so an explicit 0 is
    honored at finalize time too.
    """
    try:
        from . import tree as _tree
        t = _tree.build_tree(out_dir)
        (out_dir / "decision-tree.html").write_text(_tree.render_html(t, t["spec"]))
        s = t["summary"]
        print(f"decision tree → {out_dir / 'decision-tree.html'} "
              f"({s['attempted']} attempted · {s['accepted']} accepted · "
              f"{s['skipped']} skipped · {s['decision']})")
        events.emit("decision_tree_written", attempted=s["attempted"],
                    accepted=s["accepted"], decision=s["decision"])
    except Exception as e:
        events.emit("decision_tree_failed", detail=str(e)[:200])

    # The hand-off artifact: the final accepted edit-set with provenance + a mergeable
    # flag, so a downstream agent turns the run into a PR by reading manifest.json
    # instead of re-deriving the timeline (aro/manifest.py).
    try:
        from . import manifest as _manifest
        oq = (_manifest.DEFAULT_OUTLIER_QUARANTINE_PCT
              if outlier_quarantine_pct is None
              else float(outlier_quarantine_pct))
        m = _manifest.build_manifest(out_dir, outlier_quarantine_pct=oq)
        (out_dir / "manifest.json").write_text(
            json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        ok = sum(1 for a in m["accepted"] if a["mergeable"])
        print(f"manifest → {out_dir / 'manifest.json'} "
              f"({len(m['accepted'])} accepted · {ok} mergeable)")
    except Exception as e:
        events.emit("manifest_failed", detail=str(e)[:200])

    from .chart import svg_to_png as _svg_to_png
    svg = out_dir / "trajectory.svg"
    if svg.exists() and _svg_to_png(svg, out_dir / "trajectory.png", 1000):
        print(f"trajectory chart → {out_dir / 'trajectory.png'}")

    # The headline figure: running-best speedup vs cumulative LLM output tokens (+ every
    # candidate, off-spec marks, the untouchable-floor ceiling). Built from events.jsonl.
    try:
        from . import chart as _chart
        from . import runlog
        # NOTE: deliberately unsliced (read_events, not load_run): the perf/token figure
        # spans a resumed run's whole history — compounding carries across run_ids.
        evs = runlog.read_events(out_dir)
        (out_dir / "perf-token.svg").write_text(
            _chart.perf_token_svg(evs, out_dir.name) + "\n")
        _svg_to_png(out_dir / "perf-token.svg", out_dir / "perf-token.png", 1400)
        print(f"perf chart → {out_dir / 'perf-token.svg'}")
    except Exception as e:
        events.emit("perf_chart_failed", detail=str(e)[:160])



# --- L4b: the multi-workload campaign -------------------------------------------

def campaign(spec, *, out_dir: Path, events, workload_proposals: int = 3,
             dry_proposals: int = 3, workload_hooks: dict = None,
             **attempt_kwargs) -> tuple:
    """Run the frontier walk per WORKLOAD: the base workload first, then factory
    variants (author → qualify → walk) until `dry_proposals` CONSECUTIVE proposals
    fail qualification — that refusal chain IS exhaustion boundary 3 closing
    (docs/self-extending-search-design.md §3.2/§3.3).

    Each workload's attempt() compounds its own cumulative patch (a synthetic
    workload's wins never silently fold into the base workload's baseline);
    provenance is carried as regime `synthetic-workload` on every row and
    permtree observation. Returns ({workload: rows}, closure_state)."""
    from . import workload_factory as wfmod
    hooks = workload_hooks or {}

    all_rows = {}
    rows, _cum, base_stop = attempt(spec, out_dir=out_dir, events=events,
                                    **attempt_kwargs)
    all_rows[spec.name] = rows
    # W3's "already covered" set must be the base workload's WHOLE hot frontier
    # (profiled), not just the attempted subset — a budget-truncated base run must
    # not let a variant claim old fns as "new frontier mass" (review finding).
    covered = {r["name"] for r in rows}
    try:
        base_fns = (hooks.get("profile_fns") or wfmod._real_profile_fns)(spec)
        covered |= set(base_fns or [])
    except Exception:
        pass

    # The generation agent is hard-down (quota/auth/CLI): every workload the
    # factory would author next runs through the SAME agent, so proposing more
    # only burns retries into `author-error(2)` the slow way. Close the
    # campaign as an author error NOW — boundary 3 stays explicitly open and
    # `aro next` routes retry-factory.
    if base_stop.startswith(_GENERATOR_DOWN):
        state = "author-error(generator-down)"
        events.emit("campaign_finished", workloads=len(all_rows), state=state,
                    covered_fns=len(covered))
        return all_rows, state

    rejects = 0
    proposed = 0
    author_failures = 0
    gen_down = False
    while proposed < workload_proposals and rejects < dry_proposals:
        proposed += 1
        wname = f"v{proposed}"
        author = hooks.get("author") or wfmod.author
        # An AUTHOR failure is infrastructure (LLM timeout / crash), not a judgment
        # on proposal quality — it must never masquerade as a dry proposal. The
        # mega-evm-0703 campaign's v3 died with `claude exited 143` and was folded
        # into `state: dry`, closing coverage boundary 3 dishonestly. Retry once;
        # a second failure hands the slot back and, after 2 such double-failures,
        # aborts the factory with an explicit author-error state (boundary OPEN).
        try:
            try:
                probe_rel, diff_rel = author(spec, wname, covered)
            except Exception as e:
                events.emit("workload_author_failed", name=wname,
                            detail=str(e)[:200], will_retry=True)
                probe_rel, diff_rel = author(spec, wname, covered)
        except Exception as e:
            events.emit("workload_author_failed", name=wname,
                        detail=str(e)[:200], will_retry=False)
            _archive_rejected(out_dir, list(wfmod.workload_paths(spec.name, wname)),
                              events, reason=f"workload author failed: {str(e)[:80]}")
            author_failures += 1
            proposed -= 1  # nothing was proposed; the slot goes back
            if author_failures >= 2:
                break
            continue
        q = wfmod.qualify(spec, wname, probe_rel, diff_rel, covered_fns=covered,
                          run_diff=hooks.get("run_diff"),
                          mutate_diff=hooks.get("mutate_diff"),
                          profile_fns=hooks.get("profile_fns"), events=events)
        if not q.ok:
            _archive_rejected(out_dir, [probe_rel, diff_rel], events,
                              reason="workload failed qualification: "
                                     + "; ".join(q.reasons)[:100])
            rejects += 1
            continue
        rejects = 0
        wfmod.save(spec, q)
        wspec = wfmod.workload_spec(spec, wname, probe_rel, diff_rel)
        wout = Path(out_dir) / f"w-{wname}"
        wout.mkdir(parents=True, exist_ok=True)
        # ISOLATED event log per workload: sharing the base log would collide the
        # a<N> attempt indices across workloads and cross-corrupt the base
        # decision-tree + manifest (review finding). The base log keeps only the
        # campaign-level events (workload_registered / campaign_finished).
        from .events import EventLog
        wevents = EventLog(wout / "events.jsonl",
                           also_console=getattr(events, "also_console", False))
        wrows, _wcum, wstop = attempt(wspec, out_dir=wout, events=wevents,
                                      workload_regime="synthetic-workload",
                                      ledger_name=spec.name,
                                      **attempt_kwargs)
        _finalize_run(wout, wevents,     # each workload gets its own tree/manifest
                      outlier_quarantine_pct=getattr(
                          spec, "outlier_quarantine_pct", None))
        all_rows[wspec.name] = wrows
        covered |= {r["name"] for r in wrows}
        if wstop.startswith(_GENERATOR_DOWN):
            gen_down = True              # same agent authors the next proposal —
            break                        # stop the factory, close author-error

    # Boundary-3 closure is keyed on state == "dry" (permtree.closure): only a chain
    # of gate-REJECTED proposals may close it. Author errors leave it explicitly open.
    if gen_down:
        state = "author-error(generator-down)"
    elif rejects >= dry_proposals:
        state = "dry"
    elif author_failures >= 2:
        state = f"author-error({author_failures})"
    else:
        state = f"proposals-exhausted({proposed})"
    events.emit("campaign_finished", workloads=len(all_rows), state=state,
                covered_fns=len(covered))
    return all_rows, state
