"""The loop: the backtest orchestrator.

Freezes a baseline worktree, calibrates noise floors via A/A, then for each round
generates candidates and runs each through the two-gate evaluator, recording
everything to memory. Robust by construction: every fallible step is guarded and
a non-recoverable error returns a *partial* Report rather than crashing.
"""
from __future__ import annotations

import time

from . import eval as evalmod
from .stats import median
from .types import (GenContext, Metrics, NoiseFloors, Objective, Patch, Report,
                    Verdict)


class _NullEvents:
    """No-op event sink so the engine works without a real EventLog."""
    def emit(self, *a, **k):
        pass


def run_backtest(target, generator, memory, *, rounds, candidates_per_round,
                 aa_runs, ab_pairs, baseline_ref, events=None,
                 goal=None, stop_dry_rounds=None, read_phase=False,
                 ignore_resume_failure=False):
    events = events or _NullEvents()
    start = time.monotonic()
    log: list = []

    def elapsed():
        return time.monotonic() - start

    def finish(floors, outcomes, pareto):
        events.emit("run_finished", pareto=pareto, candidates=len(outcomes),
                    accepted=len(pareto), elapsed_s=round(elapsed(), 1))
        return Report(target=target.name, baseline_ref=baseline_ref, rounds=rounds,
                      floors=floors, outcomes=outcomes, pareto=pareto, log=log,
                      elapsed_secs=elapsed())

    events.emit("run_started", target=target.name, baseline_ref=baseline_ref,
                rounds=rounds, candidates_per_round=candidates_per_round,
                aa_runs=aa_runs, ab_pairs=ab_pairs)

    # 1) Freeze a baseline worktree.
    try:
        baseline = target.make_worktree("baseline")
    except Exception as e:
        log.append(f"make_worktree(baseline) failed: {e}")
        events.emit("error", stage="make_worktree", detail=str(e))
        return finish(NoiseFloors(), [], [])
    log.append(f"baseline worktree at {baseline}")

    # 2) Build the baseline.
    try:
        target.build(baseline)
    except Exception as e:
        log.append(f"baseline build failed: {e}")
        events.emit("error", stage="baseline_build", detail=str(e))
        target.remove_worktree(baseline)
        return finish(NoiseFloors(), [], [])
    log.append("baseline build ok")
    events.emit("baseline_built", worktree=str(baseline))

    # Regression baseline (N_pre): how many tests pass on the frozen baseline. A
    # candidate that drops below this is auto-discarded even if it still exits 0
    # (autoresearch's absolute regression gate). None → the gate degrades to off.
    try:
        n_pre = target.test(baseline)
    except Exception as e:
        n_pre = None
        log.append(f"baseline test (N_pre) failed; regression gate off: {e}")
    if n_pre is not None:
        log.append(f"regression baseline: {n_pre} tests pass")
        events.emit("regression_baseline", n_pre=n_pre)

    # Resume: rebuild the cumulative accepted patch from memory and apply it, so a
    # re-run into the same --out continues from the ADVANCED baseline (compounding
    # survives across runs), not from scratch. bench/calibrate then run on top of it.
    accepted_edits: list = memory.accepted_edits()
    if accepted_edits:
        try:
            target.apply(Patch(edits=list(accepted_edits)), baseline)
            target.build(baseline)
            log.append(f"resumed: applied {len(accepted_edits)} accepted edit(s) to baseline")
            events.emit("baseline_resumed", edits=len(accepted_edits))
        except Exception as e:
            if events:
                events.emit("error", stage="resume", detail=str(e))
            if not ignore_resume_failure:
                target.remove_worktree(baseline)
                # Fail fast: silently dropping the accepted patch would optimize the
                # ORIGINAL code while the event log / pareto claim the ADVANCED
                # baseline — the benchmarks would be incomparable and the conclusions
                # contaminated. Don't degrade quietly.
                raise RuntimeError(
                    f"resume failed: could not re-apply {len(accepted_edits)} accepted "
                    f"edit(s) to the baseline ({e}). Point --out at a fresh dir, or pass "
                    "--ignore-resume-failure to start clean on purpose.")
            accepted_edits = []
            log.append(f"resume apply failed; --ignore-resume-failure set, starting clean: {e}")

    # 3) Baseline benchmark (continue with empty metrics on failure).
    try:
        baseline_metrics = target.bench(baseline)
    except Exception as e:
        log.append(f"baseline bench failed (continuing empty): {e}")
        baseline_metrics = Metrics()

    # The observe arm: build a region hint from the baseline profile so the
    # generator gets *where the work is* (esp. allocations), not just objectives.
    def make_hint():
        rows = [f"{n}={median(baseline_metrics.get(n)):.0f}"
                for n in baseline_metrics.metric_names() if baseline_metrics.get(n)]
        if not rows:
            return None
        prof = getattr(target, "last_profile", None)
        extra = f" (~{prof[1] / 1e6:.0f}MB allocated on the hot path)" if prof else ""
        return ("baseline profile — " + "; ".join(rows) + extra
                + ". High-value lever: cut avoidable heap allocations on the "
                "update/finalize path; the allocation count is far less noisy than "
                "wall-clock, so a real reduction is easier to prove.")

    region_hint = (target.compute_region_hint(baseline)
                   if hasattr(target, "compute_region_hint") else make_hint())
    _prof = getattr(target, "last_profile", None)
    if _prof:
        events.emit("baseline_profiled", allocs=int(_prof[0]), bytes=int(_prof[1]))
        log.append(f"baseline profile: {int(_prof[0])} allocs, {int(_prof[1])} bytes")

    # 4) Objectives: declared, else every measured baseline metric (minimize).
    objs = target.objectives()
    if not objs:
        objs = [Objective(m, True) for m in baseline_metrics.metric_names()]
    log.append("objectives: " + (", ".join(o.metric for o in objs) if objs else "(none)"))

    # 5) A/A calibration of the noise floors.
    try:
        floors = evalmod.calibrate_floors(target, baseline, aa_runs, objs)
    except Exception as e:
        log.append(f"calibrate_floors failed: {e}")
        events.emit("error", stage="calibrate_floors", detail=str(e))
        target.remove_worktree(baseline)
        return finish(NoiseFloors(), [], [])
    memory.set_floors(floors)
    for m, f in floors.floors.items():
        log.append(f"floor {m}: {f:.3f}%")
    events.emit("floors_calibrated", floors=dict(floors.floors))

    # 6) The rounds: read -> generate -> judge -> record -> compound. Stops at the
    #    hard cap, when the goal target is met, or after `stop_dry_rounds`
    #    consecutive non-accepts (diminishing returns). `accepted_edits` is the
    #    cumulative accepted patch the baseline carries, so accepted optimizations
    #    compound across rounds (#5).
    outcomes = []
    dry = 0
    stop_reason = "max_rounds"
    for r in range(rounds):
        ctx = GenContext(round=r, objectives=objs, baseline=baseline_metrics,
                         memory_summary=memory.summary(), region_hint=region_hint,
                         agenda=memory.open_directions(), base_edits=list(accepted_edits))
        events.emit("round_started", round=r, accepted_so_far=len(accepted_edits),
                    memory_summary=ctx.memory_summary)

        # Read phase: a read-only "understand -> plan" step before implementing,
        # so the expensive write-loop executes a known plan rather than re-deriving.
        if read_phase and hasattr(generator, "understand"):
            ctx.plan = generator.understand(ctx)
            events.emit("read_phase", round=r, has_plan=bool(ctx.plan))
            if ctx.plan:
                log.append(f"round {r}: read-phase plan ({len(ctx.plan)} chars)")

        cands = generator.propose(ctx, candidates_per_round)
        log.append(f"round {r}: generator proposed {len(cands)} candidate(s)")
        accepted_this_round = False
        round_outcomes = []
        for cand in cands:
            events.emit("candidate_proposed", round=r, id=cand.id,
                        hypothesis=cand.hypothesis,
                        files=[e.path for e in cand.patch.edits])
            base_patch = Patch(edits=list(accepted_edits))
            outcome = evalmod.evaluate(target, baseline, base_patch, cand,
                                       ab_pairs, floors, objs, events=events, n_pre=n_pre)
            memory.record(cand, outcome)
            log.append(f"candidate {cand.id}: {outcome.verdict.value}")
            events.emit("candidate_verdict", round=r, id=cand.id,
                        verdict=outcome.verdict.value,
                        deltas=[{"metric": d.metric, "delta_pct": d.delta_pct,
                                 "ci_low_pct": d.ci_low_pct, "ci_high_pct": d.ci_high_pct,
                                 "floor_pct": d.floor_pct, "improved": d.improved}
                                for d in outcome.deltas])
            outcomes.append((cand, outcome))
            round_outcomes.append((cand, outcome))

            # #5: compound — fold an accepted patch into the working baseline so
            #     the next round optimizes (and is measured) on top of it.
            if outcome.verdict == Verdict.ACCEPTED and cand.patch.edits:
                accepted_this_round = True
                try:
                    target.apply(cand.patch, baseline)
                    target.build(baseline)
                    accepted_edits.extend(cand.patch.edits)
                    try:
                        baseline_metrics = target.bench(baseline)
                        region_hint = (target.compute_region_hint(baseline)
                                       if hasattr(target, "compute_region_hint")
                                       else make_hint())  # refresh for new baseline
                    except Exception:
                        pass  # keep prior hot metrics if the refresh bench fails
                    log.append(f"baseline advanced by {cand.id} "
                               f"(cumulative {len(accepted_edits)} edit(s))")
                    events.emit("baseline_advanced", by=cand.id,
                                cumulative_edits=len(accepted_edits),
                                files=[e.path for e in accepted_edits])
                except Exception as e:
                    log.append(f"baseline advance failed after {cand.id}: {e}")
                    events.emit("baseline_advance_failed", by=cand.id, detail=str(e))

        # Reflect: turn this round's verdicts into forward-looking research
        # directions (the agenda), so the next round carries accumulated
        # *direction*, not just dead ends. Generation-side; the judge is untouched.
        # Best-effort — a reflect failure never breaks the loop.
        if hasattr(generator, "reflect") and round_outcomes:
            try:
                upd = generator.reflect(ctx, round_outcomes)
            except Exception as e:
                upd = None
                log.append(f"round {r}: reflect failed: {e}")
            if upd:
                for rid, status in upd.get("resolve", []):
                    memory.resolve_direction(rid, status)
                    events.emit("direction_resolved", round=r, id=rid, status=status)
                items = [{"direction": a["direction"], "rationale": a["rationale"],
                          "source": f"reflect-r{r}", "round": r}
                         for a in upd.get("add", [])]
                for d in memory.add_directions(items):
                    events.emit("direction_proposed", round=r, id=d.id,
                                direction=d.direction, source=d.source)

        # --- stop conditions (end of round) ---
        dry = 0 if accepted_this_round else dry + 1
        if goal is not None and goal.target is not None:
            vals = baseline_metrics.get(goal.metric) or []
            best = median(vals) if vals else float("nan")
            met = (best <= goal.target) if goal.direction == "minimize" else (best >= goal.target)
            if best == best and met:
                stop_reason = "goal_met"
                log.append(f"goal met: {goal.metric}={best:.1f} (target {goal.target})")
                events.emit("goal_met", metric=goal.metric, value=best, target=goal.target)
                break
        if stop_dry_rounds and dry >= stop_dry_rounds:
            stop_reason = "diminishing_returns"
            log.append(f"stopping: {dry} consecutive round(s) with no accept")
            events.emit("stopped", reason="diminishing_returns", dry_rounds=dry)
            break

    log.append(f"stop reason: {stop_reason}")
    # 7) Tear down the baseline and assemble the final report.
    target.remove_worktree(baseline)
    return finish(floors, outcomes, memory.pareto_ids())
