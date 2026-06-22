"""The evaluator / 评判器: the two-gate verification.

Gate 0 (guard): reject patches that reach outside the implementation.
Gate 1 (correctness): build -> test -> differential vs the frozen baseline.
Gate 2 (significance): paired, order-alternated A/B bench -> per-metric Δ% +
bootstrap CI, checked against the A/A-calibrated noise floor; then Pareto.

The significance rule is deliberately conservative: a change counts as an
improvement only when it clears *both* a metric-specific A/A floor *and* a
bootstrap CI that excludes zero. This kills the two classic false positives —
drift between consecutive runs (cancelled by the alternated pairing) and a lucky
single sample (the CI demands the whole resampled band agree on the sign).
"""
from __future__ import annotations

from . import guard
from .stats import bootstrap_ci, median, quantile, seed_for_metric
from .types import (Candidate, EvalOutcome, MetricDelta, Metrics, NoiseFloors,
                    Objective, Verdict)


def calibrate_floors(target, baseline_work, runs: int, objectives) -> NoiseFloors:
    """A/A calibration: run the frozen baseline against *itself* to learn how much
    of the measured difference is pure machine noise. Floor per metric = the 90th
    percentile of |Δ%| across runs, clamped to a 0.5% minimum; metrics with <2
    usable samples fall back to a 2.0% default."""
    deltas: dict[str, list[float]] = {}
    for _ in range(runs):
        a = target.bench(baseline_work)
        b = target.bench(baseline_work)
        for metric in a.metric_names():
            sa, sb = a.get(metric), b.get(metric)
            if sa is None or sb is None:
                continue
            va, vb = median(sa), median(sb)
            if not _finite(va) or not _finite(vb) or va == 0.0:
                continue
            d = (vb - va) / va * 100.0
            if not _finite(d):
                continue
            deltas.setdefault(metric, []).append(abs(d))

    floors = NoiseFloors()
    covered = set()
    for metric, mags in deltas.items():
        if len(mags) < 2:
            floor = 2.0
        else:
            q90 = quantile(mags, 0.90)
            floor = max(q90, 0.5) if _finite(q90) else 2.0
        floors.put(metric, floor)
        covered.add(metric)
    for obj in objectives:
        if obj.metric not in covered:
            floors.put(obj.metric, 2.0)
            covered.add(obj.metric)
    return floors


def evaluate(target, baseline_work, base_patch, candidate: Candidate, ab_pairs: int,
             floors: NoiseFloors, objectives, events=None, n_pre=None) -> EvalOutcome:
    """Evaluate one candidate through both gates.

    `base_patch` is the cumulative already-accepted patch (the current working
    baseline; empty in round 0). The candidate is measured *on top of it*: the
    candidate worktree gets `base_patch` then `candidate.patch`, and the A/B
    baseline (`baseline_work`) already carries `base_patch` — so the Δ is the
    candidate's marginal improvement over the current best, which is what lets
    accepted optimizations compound across rounds (#5)."""

    def ev(status_event: str, **f):
        if events is not None:
            events.emit(status_event, candidate=candidate.id, **f)

    # ---- Gate 0: reward-hacking guard (no worktree needed) ------------------
    reason = guard.screen(candidate.patch, getattr(target, "regions", None))
    if reason:
        ev("gate", gate="guard", status="reject", detail=reason)
        return EvalOutcome(candidate.id, Verdict.REJECTED, [],
                           [f"reward-hacking guard: {reason}"])
    ev("gate", gate="guard", status="ok")

    # ---- Gate 1: correctness ------------------------------------------------
    try:
        work = target.make_worktree(f"cand-{candidate.id}")
    except Exception as e:
        ev("gate", gate="worktree", status="fail", detail=str(e))
        return EvalOutcome(candidate.id, Verdict.BUILD_FAILED, [],
                           [f"make_worktree failed: {e}"])

    def fail(verdict: Verdict, note: str, gate: str) -> EvalOutcome:
        ev("gate", gate=gate, status="fail", detail=note)
        target.remove_worktree(work)
        return EvalOutcome(candidate.id, verdict, [], [note])

    # Apply the already-accepted base patch first, then the candidate's own.
    try:
        target.apply(base_patch, work)
    except Exception as e:
        return fail(Verdict.BUILD_FAILED, f"apply(base) failed: {e}", "apply")
    try:
        target.apply(candidate.patch, work)
    except Exception as e:
        return fail(Verdict.BUILD_FAILED, f"apply failed: {e}", "apply")
    ev("gate", gate="apply", status="ok")
    # Measurement self-check: a candidate WITH edits MUST recompile, else the
    # bench/differential would compare a STALE binary (the shared-target-dir reuse
    # bug). Prefer a STRUCTURED guarantee — scoped-clean the edited crate so the
    # build is forced to recompile it (robust to `cargo build -q`, caches, output
    # format). Only when that can't run do we fall back to grepping stdout.
    forced = False
    if candidate.patch.edits and hasattr(target, "scoped_clean"):
        try:
            forced = target.scoped_clean(work)
        except Exception:
            forced = False
    try:
        build_out = target.build(work)
    except Exception as e:
        return fail(Verdict.BUILD_FAILED, f"build failed: {e}", "build")
    if (candidate.patch.edits and not forced
            and isinstance(build_out, str) and "Compiling" not in build_out):
        return fail(Verdict.VERIFY_FAILED,
                    "measurement-unsound: candidate did not recompile (target-dir "
                    "reuse?) — refusing to bench a stale binary", "recompile-check")
    ev("gate", gate="build", status="ok")
    try:
        n_pass = target.test(work)
    except Exception as e:
        return fail(Verdict.VERIFY_FAILED, f"tests failed: {e}", "test")
    # Regression gate (absolute, borrowed from autoresearch): even a build+test
    # that exits 0 is discarded if it drops below the baseline pass count N_pre —
    # a tempting win that silently stops running pre-existing tests is not a win.
    if n_pre is not None and n_pass is not None and n_pass < n_pre:
        return fail(Verdict.VERIFY_FAILED,
                    f"regression: {n_pass} passing tests < baseline {n_pre}",
                    "regression")
    ev("gate", gate="test", status="ok")
    # Differential is the byte-identical guarantee. STRICT by default: a target
    # without a random-input differential probe is refused — the test suite alone is
    # not a byte-identical proof (it matters for crypto/EVM/consensus). Only an
    # explicit constraints.weak_oracle=true downgrades to the test-only check, and the
    # outcome is then flagged. (MockTarget exposes neither attr → no enforcement.)
    required = getattr(target, "differential_required", False)
    has_diff = getattr(target, "has_differential", False)
    weak_oracle_note = None
    if required and not has_diff:
        return fail(Verdict.VERIFY_FAILED,
                    "no differential oracle: behaviour cannot be proven byte-identical. "
                    "Add a benchmark_probe differential, or set constraints.weak_oracle=true "
                    "to accept the weaker test-suite-only check.", "differential")
    try:
        if not target.differential(work, baseline_work):
            return fail(Verdict.VERIFY_FAILED,
                        "differential check failed: behavior differs from baseline",
                        "differential")
    except Exception as e:
        return fail(Verdict.VERIFY_FAILED, f"differential check errored: {e}", "differential")
    if required and not has_diff:
        pass  # unreachable (returned above)
    elif not has_diff:
        weak_oracle_note = ("WEAK ORACLE: no random-input differential — behaviour proven "
                            "only by the test suite, NOT byte-identical")
        ev("gate", gate="differential", status="ok-weak", detail=weak_oracle_note)
    else:
        ev("gate", gate="differential", status="ok")

    # ---- Gate 2: significance (paired A/B) ----------------------------------
    paired: dict[str, dict] = {}  # metric -> {base:[], cand:[], delta:[]}
    for i in range(ab_pairs):
        # Alternate which side runs first to cancel slow drift across the pair.
        try:
            if i % 2 == 0:
                base_m = target.bench(baseline_work)
                cand_m = target.bench(work)
            else:
                cand_m = target.bench(work)
                base_m = target.bench(baseline_work)
        except Exception as e:
            return fail(Verdict.VERIFY_FAILED, f"bench failed: {e}", "bench")

        for metric in base_m.metric_names():
            sb, sc = base_m.get(metric), cand_m.get(metric)
            if sb is None or sc is None:
                continue
            bi, ci = median(sb), median(sc)
            if not _finite(bi) or not _finite(ci) or bi == 0.0:
                continue
            di = (ci - bi) / bi * 100.0
            if not _finite(di):
                continue
            slot = paired.setdefault(metric, {"base": [], "cand": [], "delta": []})
            slot["base"].append(bi)
            slot["cand"].append(ci)
            slot["delta"].append(di)

    obj_min = {o.metric: o.minimize for o in objectives}
    objective_metrics = (list(obj_min.keys()) if objectives else list(paired.keys()))

    deltas: list[MetricDelta] = []
    notes: list[str] = []
    if weak_oracle_note:
        notes.append(weak_oracle_note)
    any_regressed = False
    any_improved = False

    for metric, p in paired.items():
        baseline = median(p["base"])
        cand_v = median(p["cand"])
        delta_pct = median(p["delta"])
        ci_low, ci_high = bootstrap_ci(p["delta"], 2000, seed_for_metric(metric))
        floor = floors.floor(metric)

        # Direction-aware: a minimize metric wins on a negative Δ%, a maximize
        # metric on a positive Δ%; the CI must agree on the winning side of 0.
        improved, regressed = _judge_metric(
            delta_pct, ci_low, ci_high, floor, obj_min.get(metric, True))

        deltas.append(MetricDelta(metric, baseline, cand_v, delta_pct,
                                  ci_low, ci_high, floor, improved, regressed))
        notes.append(
            f"A/A floor for {metric} = {floor:.2f}%; "
            f"Δ={delta_pct:+.2f}% (CI [{ci_low:+.2f}, {ci_high:+.2f}])")

        if metric in objective_metrics:
            any_regressed = any_regressed or regressed
            any_improved = any_improved or improved

    if any_regressed:
        verdict, why = Verdict.REGRESSED, "an objective metric significantly regressed"
    elif any_improved:
        verdict, why = Verdict.ACCEPTED, "an objective metric significantly improved with no regressions"
    else:
        verdict, why = Verdict.WITHIN_NOISE, "no objective metric moved beyond its noise floor"
    notes.append(f"verdict: {verdict.value} — {why}")
    ev("gate", gate="significance", status=verdict.value,
       detail="; ".join(f"{d.metric} Δ{d.delta_pct:+.2f}% CI[{d.ci_low_pct:+.2f},{d.ci_high_pct:+.2f}] floor{d.floor_pct:.2f}%"
                        for d in deltas))

    target.remove_worktree(work)
    return EvalOutcome(candidate.id, verdict, deltas, notes)


def _judge_metric(delta_pct, ci_low, ci_high, floor, minimize: bool = True):
    """(improved, regressed) for one metric, direction-aware. A win must clear the
    A/A floor AND have its bootstrap CI entirely on the winning side of zero."""
    if minimize:
        return (delta_pct < -floor and ci_high < 0.0,
                delta_pct > floor and ci_low > 0.0)
    return (delta_pct > floor and ci_low > 0.0,
            delta_pct < -floor and ci_high < 0.0)


def _finite(x) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))
