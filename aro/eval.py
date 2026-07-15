"""The evaluator: the multi-gate verification.

Gate 0 (guard): reject patches that reach outside the implementation.
Gate 1 (correctness): build -> test -> differential vs the frozen baseline.
Gate 1.5 (instruction count): deterministic whole-process Ir A/B under callgrind
  — final for CPU-bound candidates; wall-clock significance is demoted to a
  locality/memory exception channel.
Gate 2 (significance): paired, order-alternated A/B bench -> per-metric Δ% +
bootstrap CI, checked against the A/A-calibrated noise floor; then Pareto.

The significance rule is deliberately conservative: a change counts as an
improvement only when it clears *both* a metric-specific A/A floor *and* a
bootstrap CI that excludes zero. This kills the two classic false positives —
drift between consecutive runs (cancelled by the alternated pairing) and a lucky
single sample (the CI demands the whole resampled band agree on the sign).
"""
from __future__ import annotations

import dataclasses
import difflib

from . import guard
from . import icount as icmod
from .stats import bootstrap_ci, median, quantile, seed_for_metric
from .types import (Candidate, EvalOutcome, MetricDelta, NoiseFloors,
                    Verdict)


def _critic_artifact(cand) -> str:
    """A readable representation of a candidate for the semantic critic: its hypothesis +
    a compact diff of each edit (so the reviewer judges the actual change, not the blob)."""
    parts = [f"hypothesis: {cand.hypothesis}"]
    for e in cand.patch.edits:
        ud = difflib.unified_diff(e.search.splitlines(), e.replace.splitlines(),
                                  lineterm="", n=3)
        body = "\n".join(l for l in ud if not l.startswith(("--- ", "+++ ")))
        parts.append(f"# {e.path}\n{body}")
    return "\n\n".join(parts)


# Detail tails for reverify / gate-failure surfaces (keep operator-readable, not dumps).
_GATE_DETAIL_TAIL = 1000


def _gate_detail_tail(text: str, n: int = _GATE_DETAIL_TAIL) -> str:
    text = text or ""
    return text[-n:] if len(text) > n else text


def run_correctness_gates(target, work, baseline_work, *, n_pre=None,
                          test_full_cmd=None, test_full_timeout=None,
                          test_full_runner=None) -> dict:
    """Gate 1 correctness chain on a worktree where the patch is already applied.

    Order: build → test → (optional test_full) → differential vs `baseline_work`.
    Mirrors evaluate()'s Gate 1 semantics (including n_pre regression and the
    strict differential-required rule) so `aro reverify` does not reimplement
    the chain. Does NOT run the reward-hacking guard, critic, Ir, or significance.

    `test_full_cmd` / timeout / runner reuse T13b's terminal seam
    (`resolve_test_full` + `run_test_full`); omit `test_full_cmd` to skip that
    tier (legacy / no key in gates).

    Returns `{ok, failing_gate, detail, gates}` where `gates` maps each run
    step to `"ok"` / `"fail"`. On success `failing_gate` is None and `detail` is "".
    """
    gates: dict = {}

    try:
        target.build(work)
        gates["build"] = "ok"
    except Exception as e:
        gates["build"] = "fail"
        return {"ok": False, "failing_gate": "build",
                "detail": _gate_detail_tail(f"build failed: {e}"), "gates": gates}

    try:
        n_pass = target.test(work)
    except Exception as e:
        gates["test"] = "fail"
        return {"ok": False, "failing_gate": "test",
                "detail": _gate_detail_tail(f"tests failed: {e}"), "gates": gates}
    if n_pre is not None and n_pass is not None and n_pass < n_pre:
        gates["test"] = "fail"
        return {"ok": False, "failing_gate": "test",
                "detail": (f"regression: {n_pass} passing tests < baseline {n_pre}"),
                "gates": gates}
    gates["test"] = "ok"

    if test_full_cmd is not None:
        from .terminal import run_test_full
        try:
            stdout, stderr, rc = run_test_full(
                test_full_cmd, work, timeout=test_full_timeout,
                runner=test_full_runner)
        except Exception as e:
            gates["test_full"] = "fail"
            return {"ok": False, "failing_gate": "test_full",
                    "detail": _gate_detail_tail(f"test_full errored: {e}"),
                    "gates": gates}
        if rc != 0:
            combined = ((stdout or "") + "\n" + (stderr or "")).strip()
            detail = _gate_detail_tail(
                combined or f"(no output; test_full exit {rc})")
            gates["test_full"] = "fail"
            return {"ok": False, "failing_gate": "test_full",
                    "detail": detail, "gates": gates}
        gates["test_full"] = "ok"

    required = getattr(target, "differential_required", False)
    has_diff = getattr(target, "has_differential", False)
    if required and not has_diff:
        gates["differential"] = "fail"
        return {"ok": False, "failing_gate": "differential",
                "detail": ("no differential oracle: behaviour cannot be proven "
                           "byte-identical. Add a benchmark_probe differential, "
                           "or set constraints.weak_oracle=true to accept the "
                           "weaker test-suite-only check."),
                "gates": gates}
    try:
        if not target.differential(work, baseline_work):
            gates["differential"] = "fail"
            return {"ok": False, "failing_gate": "differential",
                    "detail": ("differential check failed: behavior differs "
                               "from baseline"),
                    "gates": gates}
    except Exception as e:
        gates["differential"] = "fail"
        return {"ok": False, "failing_gate": "differential",
                "detail": _gate_detail_tail(f"differential check errored: {e}"),
                "gates": gates}
    gates["differential"] = "ok"
    return {"ok": True, "failing_gate": None, "detail": "", "gates": gates}


def calibrate_floors(target, baseline_work, runs: int, objectives, scale: int = 1) -> NoiseFloors:
    """A/A calibration: run the frozen baseline against *itself* to learn how much
    of the measured difference is pure machine noise. Floor per metric = the 90th
    percentile of |Δ%| across runs, clamped to a 0.5% minimum; metrics with <2
    usable samples fall back to a 2.0% default."""
    deltas: dict[str, list[float]] = {}
    for _ in range(runs):
        a = target.bench(baseline_work, scale)
        b = target.bench(baseline_work, scale)
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


def _edit_key(patch):
    """A patch's identity by its edits' shape — so two candidates that make the SAME
    textual change dedup to one (don't pay the serial judge twice for an identical patch)."""
    return tuple(sorted((e.path, e.search, e.replace) for e in patch.edits))


def dedup_candidates(cands):
    """Drop candidates whose patch is textually identical to an earlier one (keep first).
    Pure; order-preserving."""
    seen, out = set(), []
    for c in cands:
        k = _edit_key(c.patch)
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def prescreen(target, baseline_work, base_patch, candidate, objectives, events=None,
              base_metrics=None, keep_worktree: bool = False):
    """Cheap gate BEFORE the expensive serial A/A+A/B judge (design §4.3b). Returns
    `(ok, smoke_delta, reason, work)`:
      ok          — False ONLY if the reward-hacking guard rejects OR the patch does not
                    build. A buildable patch always passes (a flaky smoke bench must NOT
                    drop a real win — smoke is for ORDERING, not rejection).
      smoke_delta — best-effort one-shot improvement % of the FIRST objective (direction-
                    aware: positive = looks like a win), or None if a smoke bench wasn't
                    obtainable. Used to PRIORITISE the serial judge queue.
      reason      — short tag for logs.
      work        — the candidate's BUILT worktree when ok and `keep_worktree` (the
                    judge reuses it instead of paying a second full build — the binary
                    in it was just compiled from exactly this candidate); else None.
    `base_metrics` shares one per-round baseline smoke bench across candidates (the
    baseline does not change mid-round; folding happens at round end).
    NO A/A, NO paired A/B, NO differential — those stay in the real judge."""
    reason_guard = guard.screen(candidate.patch, getattr(target, "regions", None))
    if reason_guard:
        return (False, None, f"guard:{reason_guard}", None)
    try:
        work = target.make_worktree(f"pre-{candidate.id}")
    except Exception as e:
        return (False, None, f"worktree:{e}", None)
    keep = False
    try:
        try:
            target.apply(base_patch, work)
            target.apply(candidate.patch, work)
        except Exception as e:
            return (False, None, f"apply:{e}", None)
        try:
            target.build(work)
        except Exception as e:
            return (False, None, f"build:{e}", None)
        # best-effort smoke Δ on the first objective (direction-aware improvement)
        smoke = None
        try:
            obj = objectives[0] if objectives else None
            if obj is not None:
                cand_m = target.bench(work, 1)
                base_m = base_metrics if base_metrics is not None \
                    else target.bench(baseline_work, 1)
                sb, sc = base_m.get(obj.metric), cand_m.get(obj.metric)
                if sb and sc:
                    bi, ci = median(sb), median(sc)
                    if _finite(bi) and _finite(ci) and bi != 0.0:
                        d = (ci - bi) / bi * 100.0          # signed Δ
                        smoke = (-d) if obj.minimize else d  # improvement (positive = win)
        except Exception:
            smoke = None
        keep = keep_worktree
        return (True, smoke, "ok", work if keep else None)
    finally:
        if not keep:
            target.remove_worktree(work)


def evaluate(target, baseline_work, base_patch, candidate: Candidate, ab_pairs: int,
             floors: NoiseFloors, objectives, events=None, n_pre=None,
             aa_runs: int = 2, bench_scales=(1,), critic=None,
             critic_context: str = "", prebuilt_work=None) -> EvalOutcome:
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
    if prebuilt_work is not None:
        # Prescreen already applied base+candidate and BUILT this worktree — the
        # binary in it is exactly this candidate's code (fresh worktree, fresh
        # per-worktree target dir), so the stale-binary recompile check is
        # satisfied by construction and the second full build is not paid.
        work = prebuilt_work
    else:
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
    if prebuilt_work is None:
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
    if prebuilt_work is None:
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
    else:
        ev("gate", gate="build", status="ok", detail="prebuilt by prescreen")

    # ---- 2nd judge: the semantic critic — AFTER apply+build, BEFORE the scarce serial
    # bench. Ordering is deliberate (no wasted spend): a candidate that doesn't even apply/build
    # is already BUILD_FAILED above, so this independent (expensive) LLM review is never
    # spent on a doomed patch — in particular one whose patch no longer applies because an
    # in-round sibling accept advanced the baseline under it. A critic reject is recorded
    # WITH its reasons (traceable) and skips the costly test + A/A + A/B + differential.
    # Two judges, AND not OR. Errors/unavailable → the critic's own default-reject decides;
    # here a None critique (the call itself threw) is treated as "no opinion" and proceeds.
    if critic is not None:
        try:
            cq = critic("code", _critic_artifact(candidate), critic_context)
        except Exception as e:
            cq = None
            if events is not None:
                events.emit("critic_error", candidate=candidate.id, detail=str(e)[:200])
        if cq is not None:
            if events is not None:
                events.emit("critic", candidate=candidate.id, id=candidate.id, kind="code",
                            verdict=cq.verdict, tokens=getattr(cq, "tokens", 0),
                            reasons=[dataclasses.asdict(rs) for rs in cq.reasons])
            if not cq.passed:
                notes = [f"critic reject [{rs.rubric}] {rs.finding}"
                         + (f" (cf. {rs.example})" if rs.example else "")
                         for rs in cq.reasons] or ["critic reject"]
                target.remove_worktree(work)
                return EvalOutcome(candidate.id, Verdict.REJECTED, [], notes,
                                   critic_rubrics=[rs.rubric for rs in cq.reasons])

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
    if not has_diff:
        weak_oracle_note = ("WEAK ORACLE: no random-input differential — behaviour proven "
                            "only by the test suite, NOT byte-identical")
        ev("gate", gate="differential", status="ok-weak", detail=weak_oracle_note)
    else:
        ev("gate", gate="differential", status="ok")

    # ---- Gate 1.5: instruction-count (Ir) — final for CPU-bound candidates ----
    # Present only when the target exposes `icount` (SpecTarget). Mock targets in
    # selftests skip this gate so existing wall-clock cases stay hermetic.
    ir_delta_pct = None
    profile_fingerprint = None
    env_fingerprint = None
    ir_notes: list[str] = []
    if hasattr(target, "icount"):
        terminal, pass_info = _run_icount_gate(
            target, baseline_work, work, candidate, events=ev)
        if terminal is not None:
            target.remove_worktree(work)
            return terminal
        # Locality passthrough: Ir notes + fingerprint ride into Gate 2.
        if pass_info:
            ir_delta_pct = pass_info.get("ir_delta_pct")
            profile_fingerprint = pass_info.get("profile_fingerprint")
            env_fingerprint = pass_info.get("env_fingerprint")
            ir_notes = list(pass_info.get("notes") or [])

    # ---- Gate 2: significance (paired A/B), with auto-tightening ------------
    # First measure at scale 1 (the floors passed in were calibrated there). If the
    # verdict is within-noise BUT some objective is NOISE-LIMITED — a consistent
    # directional effect (CI excludes 0) the floor just can't resolve — re-bench at a
    # higher ARO_BENCH_SCALE (re-calibrating the floor) so a real small win can surface.
    # Bounded by `bench_scales`, and guarded against probe-shopping: the escalated Δ
    # must AGREE IN SIGN with scale 1, and we stop escalating once the floor no longer
    # drops (a probe that ignores the scale can't be tightened → honest noise-limited).
    # Only locality-class candidates reach this gate when `icount` is available.
    obj_min = {o.metric: o.minimize for o in objectives}
    notes: list[str] = []
    if weak_oracle_note:
        notes.append(weak_oracle_note)
    notes.extend(ir_notes)

    def measure(scale, floors_at_scale):
        try:
            deltas, agg = _significance(target, baseline_work, work, ab_pairs,
                                        scale, obj_min, objectives, floors_at_scale)
        except _BenchError:
            return None, None
        return deltas, agg

    scales = list(bench_scales) or [1]
    deltas, agg = measure(scales[0], floors)
    if deltas is None:
        return fail(Verdict.VERIFY_FAILED, "bench failed", "bench")
    base_floor = floors
    si = 1
    while (not agg["improved"] and not agg["regressed"] and agg["noise_limited"]
           and si < len(scales)):
        scale = scales[si]; si += 1
        ev("bench_rescaled", from_scale=deltas[0].bench_scale if deltas else 1,
           to_scale=scale, reason="noise-limited: CI excludes 0 but |Δ| < floor")
        try:
            new_floors = calibrate_floors(target, baseline_work, aa_runs, objectives, scale)
        except Exception:
            break
        # Only worth continuing if the floor actually dropped (else the probe ignores
        # ARO_BENCH_SCALE and tightening is futile).
        if not _floor_dropped(base_floor, new_floors, obj_min):
            notes.append(f"auto-tighten stopped at scale {scale}: floor did not drop "
                         "(probe not scale-aware?)")
            break
        new_deltas, new_agg = measure(scale, new_floors)
        if new_deltas is None:
            break
        if not _sign_agrees(deltas, new_deltas, obj_min):
            notes.append(f"auto-tighten REJECTED at scale {scale}: Δ sign disagreed with "
                         "scale 1 — not a robust effect, keeping the conservative verdict")
            break
        deltas, agg, base_floor = new_deltas, new_agg, new_floors

    for d in deltas:
        notes.append(f"[scale {d.bench_scale}] A/A floor {d.metric} = {d.floor_pct:.2f}%; "
                     f"Δ={d.delta_pct:+.2f}% (CI [{d.ci_low_pct:+.2f}, {d.ci_high_pct:+.2f}])")

    if agg["regressed"]:
        verdict, why = Verdict.REGRESSED, "an objective metric significantly regressed"
    elif agg["improved"]:
        verdict, why = Verdict.ACCEPTED, "an objective metric significantly improved with no regressions"
    elif agg["noise_limited"]:
        verdict, why = (Verdict.NOISE_LIMITED,
                        "a consistent directional effect (CI excludes 0) the measurement "
                        "could not resolve above its floor even after auto-tightening")
    else:
        verdict, why = Verdict.WITHIN_NOISE, "no objective metric moved beyond its noise floor"
    notes.append(f"verdict: {verdict.value} — {why}")
    ev("gate", gate="significance", status=verdict.value,
       detail="; ".join(f"{d.metric} Δ{d.delta_pct:+.2f}% CI[{d.ci_low_pct:+.2f},{d.ci_high_pct:+.2f}] "
                        f"floor{d.floor_pct:.2f}% scale{d.bench_scale}" for d in deltas))

    target.remove_worktree(work)
    return EvalOutcome(candidate.id, verdict, deltas, notes,
                       ir_delta_pct=ir_delta_pct,
                       profile_fingerprint=profile_fingerprint,
                       env_fingerprint=env_fingerprint)


def _run_icount_gate(target, baseline_work, work, candidate, events=None,
                     version_runner=None):
    """Gate 1.5. Returns `(EvalOutcome, None)` on a terminal Ir verdict, or
    `(None, pass_info)` when a locality claim with cache evidence should continue
    into wall-clock Gate 2. `pass_info` carries ir_delta_pct / profile_fingerprint
    / env_fingerprint / notes for the final record.

    `events` is evaluate's local `ev(status_event, **fields)` closure (or None).
    `version_runner` injects tool-version probing for hermetic tests (threaded
    into `selfcheck.require_selfcheck`).
    """
    def gate_ev(**f):
        if events is not None:
            events("gate", **f)

    spec = getattr(target, "spec", None)
    probe_covers = list(getattr(spec, "probe_covers", None) or [])
    if not probe_covers and spec is not None:
        raw = getattr(spec, "raw", None) or {}
        probe_covers = list(raw.get("probe_covers") or [])
    icmod.warn_if_no_probe_covers(probe_covers)

    patched = [e.path for e in candidate.patch.edits]
    if probe_covers and not icmod.probe_covers_patch(probe_covers, patched):
        note = (f"no-coverage: patched files {patched} do not overlap "
                f"probe_covers {probe_covers}")
        gate_ev(gate="icount", status="no-coverage", detail=note)
        return (EvalOutcome(candidate.id, Verdict.NO_COVERAGE, [], [note]), None)

    # Host health precondition (same style as profile-fidelity). Missing /
    # stale / fingerprint-mismatched marker → hard error; ARO_SKIP_SELFCHECK=1
    # bypasses with a loud warning (and short-circuits BEFORE version probing).
    # env_fp rides on every Ir record when present (skip-when-absent otherwise).
    from . import selfcheck as scmod
    try:
        env_fp = scmod.require_selfcheck(spec, runner=version_runner)
    except scmod.SelfcheckError as e:
        note = str(e)
        gate_ev(gate="icount", status="fail", detail=note)
        return (EvalOutcome(candidate.id, Verdict.VERIFY_FAILED, [], [note]), None)

    locality = icmod.is_locality_claim(candidate)
    eps = icmod.ir_epsilon_pct(spec)
    # Lowest bench scale: valgrind is 10–50× slower; identical scale both sides.
    scale = 1
    try:
        base_r = target.icount(baseline_work, scale=scale, cache_sim=locality)
        cand_r = target.icount(work, scale=scale, cache_sim=locality)
    except Exception as e:
        note = f"icount failed: {e}"
        gate_ev(gate="icount", status="fail", detail=note)
        return (EvalOutcome(candidate.id, Verdict.VERIFY_FAILED, [], [note],
                            env_fingerprint=env_fp), None)

    fp = cand_r.profile_fingerprint or base_r.profile_fingerprint
    decision = icmod.judge_ir(base_r, cand_r, epsilon_pct=eps, locality=locality)
    if decision.passthrough:
        gate_ev(gate="icount", status="passthrough",
                detail=f"ΔIr={decision.ir_delta_pct:+.4f}% locality+cache")
        return (None, {
            "ir_delta_pct": decision.ir_delta_pct,
            "profile_fingerprint": fp,
            "env_fingerprint": env_fp,
            "notes": decision.notes,
        })
    gate_ev(gate="icount", status=decision.verdict.value,
            detail=f"ΔIr={decision.ir_delta_pct:+.4f}%")
    return (EvalOutcome(candidate.id, decision.verdict, decision.deltas,
                        decision.notes,
                        ir_delta_pct=decision.ir_delta_pct,
                        profile_fingerprint=fp,
                        env_fingerprint=env_fp), None)


class _BenchError(Exception):
    pass


def _significance(target, baseline_work, work, ab_pairs, scale, obj_min, objectives, floors):
    """One paired-A/B significance pass at a given ARO_BENCH_SCALE → (deltas, agg)."""
    paired: dict = {}
    for i in range(ab_pairs):
        try:
            if i % 2 == 0:
                base_m = target.bench(baseline_work, scale); cand_m = target.bench(work, scale)
            else:
                cand_m = target.bench(work, scale); base_m = target.bench(baseline_work, scale)
        except Exception as e:
            raise _BenchError(str(e))
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
            slot["base"].append(bi); slot["cand"].append(ci); slot["delta"].append(di)

    objective_metrics = (list(obj_min.keys()) if objectives else list(paired.keys()))
    deltas: list = []
    agg = {"improved": False, "regressed": False, "noise_limited": False}
    for metric, p in paired.items():
        baseline = median(p["base"]); cand_v = median(p["cand"]); delta_pct = median(p["delta"])
        ci_low, ci_high = bootstrap_ci(p["delta"], 2000, seed_for_metric(metric))
        floor = floors.floor(metric)
        improved, regressed = _judge_metric(delta_pct, ci_low, ci_high, floor,
                                            obj_min.get(metric, True))
        nl = (not improved and not regressed
              and ((ci_low > 0 and ci_high > 0) or (ci_low < 0 and ci_high < 0)))
        deltas.append(MetricDelta(metric, baseline, cand_v, delta_pct, ci_low, ci_high,
                                  floor, improved, regressed, noise_limited=nl, bench_scale=scale))
        if metric in objective_metrics:
            agg["regressed"] = agg["regressed"] or regressed
            agg["improved"] = agg["improved"] or improved
            agg["noise_limited"] = agg["noise_limited"] or nl
    return deltas, agg


def _floor_dropped(old: NoiseFloors, new: NoiseFloors, obj_min: dict, frac: float = 0.8) -> bool:
    """True if the new (higher-scale) floor is meaningfully below the old for some
    objective metric — i.e. tightening actually bought measurement power."""
    for m in obj_min:
        o, n = old.floor(m), new.floor(m)
        if _finite(o) and _finite(n) and n < o * frac:
            return True
    return False


def _sign_agrees(d1: list, d2: list, obj_min: dict) -> bool:
    """Anti-probe-shopping: the escalated Δ must agree in sign with scale 1 for every
    objective metric (a win that only appears at higher scale is suspect)."""
    a = {d.metric: d.delta_pct for d in d1}
    b = {d.metric: d.delta_pct for d in d2}
    for m in obj_min:
        if m in a and m in b and a[m] != 0 and b[m] != 0:
            if (a[m] > 0) != (b[m] > 0):
                return False
    return True


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
