"""The loop: the backtest orchestrator.

Freezes a baseline worktree, calibrates noise floors via A/A, then for each round
generates candidates and runs each through the two-gate evaluator, recording
everything to memory. Robust by construction: every fallible step is guarded and
a non-recoverable error returns a *partial* Report rather than crashing.

Structure (the infinite-flow Phase-2 seam): generation (`generator.propose`) and
judging (`_judge_round`) touch no shared mutable state beyond explicit arguments —
a producer-consumer queue can be inserted between them without re-plumbing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import eval as evalmod
from .stats import median
from .types import (EvalOutcome, GenContext, Metrics, NoiseFloors, Objective,
                    Patch, Report, Verdict, best_improvement)


class _NullEvents:
    """No-op event sink so the engine works without a real EventLog."""
    def emit(self, *a, **k):
        pass


@dataclass
class RunConfig:
    """Every knob of one backtest run — what used to be 13 threaded kwargs."""
    rounds: int
    candidates_per_round: int
    aa_runs: int
    ab_pairs: int
    baseline_ref: str
    goal: object = None
    stop_dry_rounds: object = None
    read_phase: bool = False
    ignore_resume_failure: bool = False
    bench_scales: tuple = (1,)
    prescreen: bool = False
    critic: object = None
    critic_context: str = ""


def _improvement(outcome, objectives) -> float:
    """Best direction-aware improvement (%) across a candidate's objective metrics — used
    to rank a round's accepts so the strongest is folded first (rule: types.best_improvement)."""
    obj_min = {o.metric: o.minimize for o in objectives}
    b = best_improvement(outcome.deltas, obj_min)
    return max(0.0, b[1]) if b else 0.0


def run_backtest(target, generator, memory, *, rounds, candidates_per_round,
                 aa_runs, ab_pairs, baseline_ref, events=None,
                 goal=None, stop_dry_rounds=None, read_phase=False,
                 ignore_resume_failure=False, bench_scales=(1,), prescreen: bool = False,
                 critic=None, critic_context=""):
    cfg = RunConfig(rounds=rounds, candidates_per_round=candidates_per_round,
                    aa_runs=aa_runs, ab_pairs=ab_pairs, baseline_ref=baseline_ref,
                    goal=goal, stop_dry_rounds=stop_dry_rounds, read_phase=read_phase,
                    ignore_resume_failure=ignore_resume_failure,
                    bench_scales=bench_scales, prescreen=prescreen,
                    critic=critic, critic_context=critic_context)
    return _Backtest(target, generator, memory, cfg, events or _NullEvents()).run()


@dataclass
class _Backtest:
    """One run's state + the phase methods. The phases mirror the loop diagram:
    freeze → resume → observe → calibrate → (read → generate → prescreen → judge →
    fold → reflect → stop?)* → teardown."""
    target: object
    generator: object
    memory: object
    cfg: RunConfig
    events: object
    log: list = field(default_factory=list)

    # --- lifecycle -------------------------------------------------------------

    def run(self):
        self._start = time.monotonic()
        cfg = self.cfg
        self.events.emit("run_started", target=self.target.name,
                         baseline_ref=cfg.baseline_ref, rounds=cfg.rounds,
                         candidates_per_round=cfg.candidates_per_round,
                         aa_runs=cfg.aa_runs, ab_pairs=cfg.ab_pairs)

        if not self._freeze_baseline():
            return self._finish(NoiseFloors(), [], [])
        teardown = True
        try:
            self._resume()                  # raises on unrecoverable resume failure
            self._observe()
            if not self._calibrate():
                teardown = False
                self.target.remove_worktree(self.baseline)
                return self._finish(NoiseFloors(), [], [])
            outcomes, stop_reason = self._rounds()
            self.log.append(f"stop reason: {stop_reason}")
        finally:
            if teardown:
                self.target.remove_worktree(self.baseline)
        # `accepted_edits[seed_n:]` is exactly what THIS run folded (past the resumed
        # seed) — the meta-loop adopts it.
        return self._finish(self.floors, outcomes, self.memory.pareto_ids(),
                            self.accepted_edits[self.seed_n:])

    def _elapsed(self):
        return time.monotonic() - self._start

    def _finish(self, floors, outcomes, pareto, folded=()):
        # Final operator checkpoint: same payload shape as mid-run `round_started`
        # (memory_summary + accepted_so_far). Dedup: skip when the last round's
        # results were already flushed by a subsequent round_started (summary
        # unchanged since that emit). Without this, a last-round accept is
        # silently swallowed — no later round_started ever carries it.
        fields = dict(pareto=pareto, candidates=len(outcomes),
                      accepted=len(pareto), elapsed_s=round(self._elapsed(), 1))
        mem = getattr(self, "memory", None)
        if mem is not None:
            summary = mem.summary()
            if summary != getattr(self, "_checkpointed_summary", None):
                fields["memory_summary"] = summary
                fields["accepted_so_far"] = len(getattr(self, "accepted_edits", []) or [])
        self.events.emit("run_finished", **fields)
        return Report(target=self.target.name, baseline_ref=self.cfg.baseline_ref,
                      rounds=self.cfg.rounds, floors=floors, outcomes=outcomes,
                      pareto=pareto, log=self.log, elapsed_secs=self._elapsed(),
                      folded_edits=list(folded))

    # --- setup phases ------------------------------------------------------------

    def _freeze_baseline(self) -> bool:
        """1) Freeze a baseline worktree; 2) build it; 3) count passing tests (N_pre)."""
        try:
            self.baseline = self.target.make_worktree("baseline")
        except Exception as e:
            self.log.append(f"make_worktree(baseline) failed: {e}")
            self.events.emit("error", stage="make_worktree", detail=str(e))
            return False
        self.log.append(f"baseline worktree at {self.baseline}")

        try:
            self.target.build(self.baseline)
        except Exception as e:
            self.log.append(f"baseline build failed: {e}")
            self.events.emit("error", stage="baseline_build", detail=str(e))
            self.target.remove_worktree(self.baseline)
            return False
        self.log.append("baseline build ok")
        self.events.emit("baseline_built", worktree=str(self.baseline))

        # Regression baseline (N_pre): how many tests pass on the frozen baseline. A
        # candidate that drops below this is auto-discarded even if it still exits 0
        # (autoresearch's absolute regression gate). None → the gate degrades to off.
        try:
            self.n_pre = self.target.test(self.baseline)
        except Exception as e:
            self.n_pre = None
            self.log.append(f"baseline test (N_pre) failed; regression gate off: {e}")
        if self.n_pre is not None:
            self.log.append(f"regression baseline: {self.n_pre} tests pass")
            self.events.emit("regression_baseline", n_pre=self.n_pre)
        return True

    def _resume(self) -> None:
        """Rebuild the cumulative accepted patch from memory and apply it, so a re-run
        into the same --out continues from the ADVANCED baseline (compounding survives
        across runs). bench/calibrate then run on top of it.

        Applies edits one-by-one in acceptance order (pareto append order — same
        source as the manifest ``acceptance_seq``). A mid-chain failure emits
        ``resume_degraded`` and continues on the last-good prefix; only a total
        failure (zero edits applied) raises the legacy hard error."""
        chain = (self.memory.accepted_edit_chain()
                 if hasattr(self.memory, "accepted_edit_chain")
                 else [(None, e) for e in self.memory.accepted_edits()])
        applied = []
        if chain:
            failed_at = None  # (idx, cand_id, path, exc)
            for i, (cid, edit) in enumerate(chain):
                try:
                    self.target.apply(Patch(edits=[edit]), self.baseline)
                    applied.append(edit)
                except Exception as e:
                    failed_at = (i, cid or "?", edit.path, e)
                    break
            if failed_at is None:
                self.target.build(self.baseline)
                self.accepted_edits = applied
                self.log.append(f"resumed: applied {len(self.accepted_edits)} "
                                f"accepted edit(s) to baseline")
                self.events.emit("baseline_resumed", edits=len(self.accepted_edits))
            else:
                i, cid, path, exc = failed_at
                n_ok = len(applied)
                n_total = len(chain)
                detail = (f"edit {i + 1}/{n_total} candidate={cid} file={path} "
                          f"failed after {n_ok} clean apply(s): {exc}")
                self.events.emit("error", stage="resume", detail=detail)
                if self.cfg.ignore_resume_failure:
                    self.accepted_edits = []
                    self.log.append(f"resume apply failed; --ignore-resume-failure set, "
                                    f"starting clean: {detail}")
                elif n_ok == 0:
                    # Total failure — legacy hard error (now names the failing edit).
                    raise RuntimeError(
                        f"resume failed: could not re-apply {n_total} "
                        f"accepted edit(s) to the baseline ({detail}). "
                        f"Point --out at a fresh dir, or pass "
                        f"--ignore-resume-failure to start clean on purpose.")
                else:
                    # Partial success: keep the prefix, continue the attempt.
                    try:
                        self.target.build(self.baseline)
                    except Exception as be:
                        # Prefix applied but build fails → treat as total failure.
                        raise RuntimeError(
                            f"resume failed: applied {n_ok}/{n_total} edit(s) but "
                            f"baseline build failed ({be}); first failing edit was "
                            f"candidate={cid} file={path}. Point --out at a fresh "
                            f"dir, or pass --ignore-resume-failure to start clean.") from be
                    self.accepted_edits = applied
                    self.log.append(f"resume degraded: {detail}")
                    self.events.emit("resume_degraded",
                                     failed_candidate=cid,
                                     failed_file=path,
                                     failed_index=i,
                                     applied=n_ok,
                                     total=n_total,
                                     detail=detail)
        else:
            self.accepted_edits = []
        # Edits present BEFORE this run's rounds (the resumed seed). Anything appended
        # past this index is what THIS run folded — reported as `folded_edits`.
        self.seed_n = len(self.accepted_edits)

    def _observe(self) -> None:
        """Baseline bench + the observe arm's region hint (profiler-grounded)."""
        try:
            self.baseline_metrics = self.target.bench(self.baseline)
        except Exception as e:
            self.log.append(f"baseline bench failed (continuing empty): {e}")
            self.baseline_metrics = Metrics()

        self.region_hint = (self.target.compute_region_hint(self.baseline)
                            if hasattr(self.target, "compute_region_hint")
                            else self._fallback_hint())
        prof = getattr(self.target, "last_profile", None)
        if prof:
            self.events.emit("baseline_profiled", allocs=int(prof[0]), bytes=int(prof[1]))
            self.log.append(f"baseline profile: {int(prof[0])} allocs, {int(prof[1])} bytes")

        # Objectives: declared, else every measured baseline metric (minimize).
        self.objs = self.target.objectives()
        if not self.objs:
            self.objs = [Objective(m, True) for m in self.baseline_metrics.metric_names()]
        self.log.append("objectives: " + (", ".join(o.metric for o in self.objs)
                                          if self.objs else "(none)"))

    def _fallback_hint(self):
        rows = [f"{n}={median(self.baseline_metrics.get(n)):.0f}"
                for n in self.baseline_metrics.metric_names()
                if self.baseline_metrics.get(n)]
        if not rows:
            return None
        prof = getattr(self.target, "last_profile", None)
        extra = f" (~{prof[1] / 1e6:.0f}MB allocated on the hot path)" if prof else ""
        return ("baseline profile — " + "; ".join(rows) + extra
                + ". High-value lever: cut avoidable heap allocations on the "
                "update/finalize path; the allocation count is far less noisy than "
                "wall-clock, so a real reduction is easier to prove.")

    def _calibrate(self) -> bool:
        """A/A calibration of the noise floors."""
        try:
            self.floors = evalmod.calibrate_floors(self.target, self.baseline,
                                                   self.cfg.aa_runs, self.objs)
        except Exception as e:
            self.log.append(f"calibrate_floors failed: {e}")
            self.events.emit("error", stage="calibrate_floors", detail=str(e))
            return False
        self.memory.set_floors(self.floors)
        for m, f in self.floors.floors.items():
            self.log.append(f"floor {m}: {f:.3f}%")
        self.events.emit("floors_calibrated", floors=dict(self.floors.floors))
        return True

    # --- the rounds ---------------------------------------------------------------

    def _rounds(self):
        """read → generate → prescreen → judge → fold → reflect, with the stop rules.
        Accepted patches compound into the working baseline (#5)."""
        outcomes = []
        dry = 0
        stop_reason = "max_rounds"
        # Tracks the memory_summary last flushed as an operator checkpoint via
        # round_started. _finish compares against this so a last-round accept is
        # not double-reported when a subsequent round_started already carried it.
        self._checkpointed_summary = None
        for r in range(self.cfg.rounds):
            # out_dir for agent-transcripts/ — EventLog.path is events.jsonl.
            _ev_path = getattr(self.events, "path", None)
            _out_dir = Path(_ev_path).parent if _ev_path else None
            ctx = GenContext(round=r, objectives=self.objs,
                             baseline=self.baseline_metrics,
                             memory_summary=self.memory.summary(),
                             region_hint=self.region_hint,
                             agenda=self.memory.open_directions(),
                             base_edits=list(self.accepted_edits),
                             emit=self.events.emit,
                             out_dir=_out_dir)
            self.events.emit("round_started", round=r,
                             accepted_so_far=len(self.accepted_edits),
                             memory_summary=ctx.memory_summary)
            self._checkpointed_summary = ctx.memory_summary

            cands = self._generate(ctx, r)
            round_outcomes = self._judge_round(r, cands, outcomes)
            accepted_this_round = self._fold_round(round_outcomes)
            self._reflect(ctx, r, round_outcomes)

            # --- stop conditions (end of round) ---
            dry = 0 if accepted_this_round else dry + 1
            goal = self.cfg.goal
            if goal is not None and goal.target is not None:
                vals = self.baseline_metrics.get(goal.metric) or []
                best = median(vals) if vals else float("nan")
                met = (best <= goal.target if goal.direction == "minimize"
                       else best >= goal.target)
                if best == best and met:
                    stop_reason = "goal_met"
                    self.log.append(f"goal met: {goal.metric}={best:.1f} "
                                    f"(target {goal.target})")
                    self.events.emit("goal_met", metric=goal.metric, value=best,
                                     target=goal.target)
                    break
            if self.cfg.stop_dry_rounds and dry >= self.cfg.stop_dry_rounds:
                stop_reason = "diminishing_returns"
                self.log.append(f"stopping: {dry} consecutive round(s) with no accept")
                self.events.emit("stopped", reason="diminishing_returns", dry_rounds=dry)
                break
        return outcomes, stop_reason

    def _generate(self, ctx, r):
        """Read phase (optional) + propose. Generation-side only — no judge state."""
        if self.cfg.read_phase and hasattr(self.generator, "understand"):
            ctx.plan, plan_tokens = self.generator.understand(ctx)
            self.events.emit("read_phase", round=r, has_plan=bool(ctx.plan),
                             tokens=plan_tokens)
            if ctx.plan:
                self.log.append(f"round {r}: read-phase plan ({len(ctx.plan)} chars)")
        cands = self.generator.propose(ctx, self.cfg.candidates_per_round)
        self.log.append(f"round {r}: generator proposed {len(cands)} candidate(s)")
        return cands

    def _prescreen(self, r, cands, outcomes):
        """Cheap gate + dedup + priority order before the scarce serial judge
        (design §4.3b). Dropped candidates are RECORDED, never silently vanished."""
        base_patch_pre = Patch(edits=list(self.accepted_edits))
        cands = evalmod.dedup_candidates(cands)
        # One baseline smoke bench per ROUND (the baseline can't move mid-round).
        try:
            base_smoke = self.target.bench(self.baseline, 1)
        except Exception:
            base_smoke = None
        survivors = []
        for c in cands:
            ok, sd, why, work = evalmod.prescreen(
                self.target, self.baseline, base_patch_pre, c, self.objs, self.events,
                base_metrics=base_smoke, keep_worktree=True)
            if work is not None:
                self._prebuilt[c.id] = work
            self.events.emit("prescreen", round=r, id=c.id, ok=ok,
                             smoke_delta=(round(sd, 3) if isinstance(sd, (int, float))
                                          else None),
                             reason=why)
            if ok:
                survivors.append((c, sd))
            else:
                # No silent caps: a dropped candidate is recorded as a failed outcome so
                # it shows up in the run-log / decision tree, not silently vanished.
                drop_v = (Verdict.REJECTED if why.startswith("guard:")
                          else Verdict.BUILD_FAILED)
                o = EvalOutcome(c.id, drop_v, [], [f"prescreen drop: {why}"])
                self.memory.record(c, o)
                outcomes.append((c, o))
        survivors.sort(key=lambda t: (t[1] if isinstance(t[1], (int, float))
                                      else float("-inf")), reverse=True)
        ordered = [c for c, _ in survivors]
        self.events.emit("prescreen_ordered", round=r,
                         order=[c.id for c in ordered],
                         smoke=[round(sd, 3) if isinstance(sd, (int, float)) else None
                                for _, sd in survivors])
        return ordered

    def _judge_round(self, r, cands, outcomes):
        """The serial judge over this round's candidates (bench never parallel)."""
        self._prebuilt = {}
        if self.cfg.prescreen and len(cands) > 1:
            cands = self._prescreen(r, cands, outcomes)
        round_outcomes = []
        try:
            return self._judge_candidates(r, cands, outcomes, round_outcomes)
        finally:
            # Even if evaluate() raises out (it is designed not to), no prescreen
            # worktree may leak — the popped one is evaluate's to remove; the rest
            # are ours.
            for leftover in self._prebuilt.values():
                self.target.remove_worktree(leftover)
            self._prebuilt = {}

    def _judge_candidates(self, r, cands, outcomes, round_outcomes):
        for cand in cands:
            self.events.emit("candidate_proposed", round=r, id=cand.id,
                             hypothesis=cand.hypothesis,
                             lens=getattr(cand, "lens", None),
                             tokens=getattr(cand, "tokens", None),
                             cost_usd=getattr(cand, "cost_usd", None),
                             files=[e.path for e in cand.patch.edits])
            # The SECOND judge (semantic critic) runs INSIDE evaluate — after the cheap
            # apply+build gate, before the scarce serial A/A+A/B bench. Two judges,
            # AND not OR.
            base_patch = Patch(edits=list(self.accepted_edits))
            outcome = evalmod.evaluate(self.target, self.baseline, base_patch, cand,
                                       self.cfg.ab_pairs, self.floors, self.objs,
                                       events=self.events, n_pre=self.n_pre,
                                       aa_runs=self.cfg.aa_runs,
                                       bench_scales=self.cfg.bench_scales,
                                       critic=self.cfg.critic,
                                       critic_context=self.cfg.critic_context,
                                       prebuilt_work=self._prebuilt.pop(cand.id, None))
            self.memory.record(cand, outcome)
            self.log.append(f"candidate {cand.id}: {outcome.verdict.value}")
            self.events.emit("candidate_verdict", round=r, id=cand.id,
                             verdict=outcome.verdict.value,
                             deltas=[{"metric": d.metric, "delta_pct": d.delta_pct,
                                      "ci_low_pct": d.ci_low_pct,
                                      "ci_high_pct": d.ci_high_pct,
                                      "floor_pct": d.floor_pct, "improved": d.improved}
                                     for d in outcome.deltas])
            outcomes.append((cand, outcome))
            round_outcomes.append((cand, outcome))
        return round_outcomes

    def _fold_round(self, round_outcomes) -> bool:
        """#5: compound — fold accepted patches into the working baseline at ROUND END
        (siblings compete fairly against the SAME frozen base). Fold greedily by
        measured improvement; a conflicting sibling keeps its honest verdict but is
        recorded superseded rather than apply-failed."""
        from .types import is_accept_verdict
        accepts = sorted(
            [(c, o) for c, o in round_outcomes
             if is_accept_verdict(o.verdict) and c.patch.edits],
            key=lambda co: _improvement(co[1], self.objs), reverse=True)
        folded_now = False
        for cand, outcome in accepts:
            try:
                self.target.apply(cand.patch, self.baseline)
                self.target.build(self.baseline)
                self.accepted_edits.extend(cand.patch.edits)
                folded_now = True
                self.log.append(f"baseline advanced by {cand.id} "
                                f"(cumulative {len(self.accepted_edits)} edit(s))")
                self.events.emit("baseline_advanced", by=cand.id,
                                 cumulative_edits=len(self.accepted_edits),
                                 files=[e.path for e in self.accepted_edits])
            except Exception as e:
                self.log.append(f"candidate {cand.id}: accepted but superseded "
                                f"(not folded): {e}")
                self.events.emit("candidate_superseded", id=cand.id,
                                 detail=str(e)[:140])
        if folded_now:
            try:
                self.baseline_metrics = self.target.bench(self.baseline)
                self.region_hint = (self.target.compute_region_hint(self.baseline)
                                    if hasattr(self.target, "compute_region_hint")
                                    else self._fallback_hint())
            except Exception:
                pass  # keep prior hot metrics if the refresh bench fails
        return folded_now

    def _reflect(self, ctx, r, round_outcomes) -> None:
        """Turn this round's verdicts into forward-looking research directions (the
        agenda). Generation-side; best-effort — a reflect failure never breaks the loop."""
        if not (hasattr(self.generator, "reflect") and round_outcomes):
            return
        try:
            upd = self.generator.reflect(ctx, round_outcomes)
        except Exception as e:
            upd = None
            self.log.append(f"round {r}: reflect failed: {e}")
        if not upd:
            return
        self.events.emit("reflect", round=r, tokens=upd.get("_tokens", 0))
        for rid, status in upd.get("resolve", []):
            self.memory.resolve_direction(rid, status)
            self.events.emit("direction_resolved", round=r, id=rid, status=status)
        items = [{"direction": a["direction"], "rationale": a["rationale"],
                  "source": f"reflect-r{r}", "round": r}
                 for a in upd.get("add", [])]
        for d in self.memory.add_directions(items):
            self.events.emit("direction_proposed", round=r, id=d.id,
                             direction=d.direction, source=d.source)
