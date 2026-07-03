# Judge protocol (the deterministic core)

This is the part that must be executed code, never model reasoning: the moat. It is what `autoresearch`-style "single float from stdout" evaluation lacks, and what makes a verdict on a sub-1% change trustworthy. Code: `aro/eval.py`, `aro/stats.py`, `aro/guard.py`.

A candidate is scored through gates, in order. Any earlier failure short-circuits.

## Gate 0: reward-hacking guard (`guard.py`)

Path-only screen, run before any build. Reject (verdict `rejected`) if the patch touches `Cargo.toml`/`Cargo.lock` (swap-in-a-library), `benches/` or `tests/` (the ruler and the judge), escapes the worktree (absolute path / `..`), or edits a file outside the spec's editable `regions` (when declared: enforces "a new target limits the edit surface by spec"). Cheap, language-agnostic, impossible to argue with.

## Gate 1: correctness (hard, before any speed)

In a fresh worktree built from the frozen baseline: apply (base patch + candidate) → `build` (+ a recompile self-check) → `test` → **regression gate** → `differential`. Any failure → `build-failed` / `verify-failed`, discarded. The regression gate is absolute (from autoresearch): the candidate must keep at least the baseline's passing-test count `N_pre`: a build that exits 0 but silently runs fewer tests is discarded (`N_pre` is parsed once on the baseline; if it can't be parsed the gate degrades to off). Differential is the byte-identical guarantee that matters for crypto/EVM: when the spec declares a `differential` probe, ARO runs that same deterministic random-input probe in the baseline and the candidate worktrees and requires identical output (a real behaviour check beyond the tests: e.g. feed many pseudo-random inputs through the hot function and fingerprint every result); a differential is **required by default**: a target with no differential probe is refused (`verify-failed`: the test suite alone is not a byte-identical proof), unless `constraints.weak_oracle=true` explicitly downgrades to the test-suite-only check, in which case the verdict is flagged `WEAK ORACLE`. When a change's safety rests on an invariant (e.g. a per-opcode fan-out check narrowed to one dimension because only that one can change here), the differential must be **adversarial**: its inputs must exercise exactly the paths the invariant argument depends on (push the state you claim "can't change here" to and over its limit), not just the happy path; a happy-path-only differential rubber-stamps an unproven assumption. This is what lets the generator safely adopt a high-leverage invariant-guarded elimination instead of retreating to a trivial change (see `optimization-lenses.md`).

## Gate 2: significance (only if correct)

- **Paired, order-alternated A/B.** For `ab_pairs` pairs, bench baseline and candidate back-to-back, alternating which runs first to cancel slow machine drift. Per pair, per metric: `Δ% = (cand - base)/base·100`.
- **The rule (direction-aware, per `Objective.minimize`):** for a **minimize** metric, **improved** iff `Δ% < -floor` AND the bootstrap CI's upper bound `< 0`; **regressed** iff `Δ% > floor` AND CI lower bound `> 0`. For a **maximize** metric the winning sign flips (improved iff `Δ% > floor` AND CI lower `> 0`). Else **within-noise**.
  - `floor` = the A/A-calibrated noise floor for that metric.
  - CI = ~95% bootstrap over the paired Δ% values (`stats.bootstrap_ci`, seeded → reproducible).
- **Verdict over objective metrics:** any objective regressed → `regressed`; else any improved (none regressed) → `accepted`; else `within-noise`, UNLESS auto-tightening (below) resolves a noise-limited objective.

So a candidate is `accepted` only when it **both** beats the run-to-run noise **and** the resampled band agrees on the sign, killing the two classic false positives (drift, and a lucky single sample).

### Auto-tightening a noise-limited result (don't give up: measure better)

A within-noise verdict can hide a real win the *measurement* is too coarse to resolve: when a metric's **CI excludes 0** (a consistent direction) but **`|Δ| < floor`**, the effect is real but the floor is too high: a `noise-limited` state, not a non-win. (The classic cause: a tiny per-call cost where scheduler/frequency jitter dominates the A/A floor.) The judge then **re-benches at a higher `ARO_BENCH_SCALE`** (a scale-aware probe multiplies its batch, so each sample averages more work and the floor drops), re-calibrates the floor, and re-judges, bounded by `run.bench_scales` (default `[1, 8, 64]`). It resolves to `accepted`/`regressed` if a tighter floor lets the signal clear, else terminates at the honest `noise-limited` verdict.

This is "write your own measurement", but disciplined so it can't game itself: (1) tightening only adds measurement power: same path, same inputs (the `ARO_BENCH_SCALE` convention multiplies the batch, never changes the workload); (2) the differential still gates behaviour; (3) **sign-agreement guard**: the escalated Δ must agree in sign with scale 1, so a "win" that only appears under tightening is rejected; (4) escalation **stops once the floor stops dropping** (a probe that ignores the scale can't be tightened → honest `noise-limited`). The judge's "can't be gamed" discipline extends to the ruler it builds for itself.

## Why this and not a single number

`autoresearch` reads one float from `verify_cmd` and compares; that works when wins are huge (2008ms→646ms) and noise is irrelevant. Our wins can be small on a noisy benchmark: a single float there is fooled or gamed by noise. The A/A floor + paired A/B + CI is exactly the machinery that tells a real gain apart from luck. But the judge is only as sound as the measurement feeding it: a shared-`CARGO_TARGET_DIR` bug once made the baseline and candidate compile to the SAME binary, masking a real ~14% speedup as `within-noise`, fixed by per-worktree target dirs. The lesson: every link in the chain (compile isolation included), not just the statistics, has to be right.

## Measurement soundness (the judge self-checks itself)

The judge is only as trustworthy as the binaries it benches: two self-checks, both learned the hard way:
- **Per-worktree target dirs.** Baseline and candidate compile to SEPARATE `CARGO_TARGET_DIR`s. A shared one makes cargo reuse the first worktree's build for the others, so baseline and candidate bench the SAME binary and every Δ collapses to ≈0: a real −53% regression and a real +14% win *both* read as within-noise. Non-negotiable.
- **Forced recompile.** Before building a candidate with edits, the judge runs a scoped `cargo clean -p <pkg>` on the edited crate, so the build is *forced* to recompile it: a structured guarantee robust to `cargo build -q`, caches, and output format (rather than grepping stdout). When a scoped clean can't run, it falls back to the older heuristic: a build that emitted no `Compiling` line reused a stale binary → rejected as `measurement-unsound`, never benched.

Lesson: every link in the chain (compile isolation included) has to be right, or the statistics are confidently meaningless.

## Stop / goal (`eval` + `engine`)

The judge also answers "are we done?": the goal (`metric`, `direction`, optional `target`) and stop (`max_rounds`, `dry_rounds`) make stopping an explicit decision, not a fixed loop count.
