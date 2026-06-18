# Judge protocol (the deterministic core)

This is the part that must be executed code, never model reasoning — the moat. It is what `autoresearch`-style "single float from stdout" evaluation lacks, and what makes a verdict on a sub-1% change trustworthy. Code: `aro/eval.py`, `aro/stats.py`, `aro/guard.py`.

A candidate is scored through gates, in order. Any earlier failure short-circuits.

## Gate 0 — reward-hacking guard (`guard.py`)

Path-only screen, run before any build. Reject (verdict `rejected`) if the patch touches `Cargo.toml`/`Cargo.lock` (swap-in-a-library), `benches/` or `tests/` (the ruler and the judge), or escapes the worktree (absolute path / `..`). Cheap, language-agnostic, impossible to argue with.

## Gate 1 — correctness (hard, before any speed)

In a fresh worktree built from the frozen baseline: apply (base patch + candidate) → `build` → `test` → **regression gate** → `differential`. Any failure → `build-failed` / `verify-failed`, discarded. The regression gate is absolute (from autoresearch): the candidate must keep at least the baseline's passing-test count `N_pre` — a build that exits 0 but silently runs fewer tests is discarded (`N_pre` is parsed once on the baseline; if it can't be parsed the gate degrades to off). Differential is the byte-identical guarantee that matters for crypto/EVM; today it leans on the test suite (MVP), with random-input differential fuzz as a `probes/` TODO.

## Gate 2 — significance (only if correct)

- **Paired, order-alternated A/B.** For `ab_pairs` pairs, bench baseline and candidate back-to-back, alternating which runs first to cancel slow machine drift. Per pair, per metric: `Δ% = (cand - base)/base·100`.
- **The rule:** a metric **improved** iff `Δ% < -floor` AND its bootstrap CI's upper bound `< 0`; **regressed** iff `Δ% > floor` AND CI lower bound `> 0`; else **within-noise**.
  - `floor` = the A/A-calibrated noise floor for that metric.
  - CI = ~95% bootstrap over the paired Δ% values (`stats.bootstrap_ci`, seeded → reproducible).
- **Verdict over objective metrics:** any objective regressed → `regressed`; else any improved (none regressed) → `accepted`; else `within-noise`.

So a candidate is `accepted` only when it **both** beats the run-to-run noise **and** the resampled band agrees on the sign — killing the two classic false positives (drift, and a lucky single sample).

## Why this and not a single number

`autoresearch` reads one float from `verify_cmd` and compares; that works when wins are huge (2008ms→646ms) and noise is irrelevant. Our wins are often <1% on a noisy benchmark — a single float there is fooled or gamed by noise. The A/A floor + paired A/B + CI is exactly the machinery that lets a small, real gain be told apart from luck. It also independently caught the layout trap an expert audit (Plainshift) had to warn about: an inline-K precompute that *looks* like a win measured `within-noise` because the bigger table entry's cache cost ate the saving — zero false wins.

## Stop / goal (`eval` + `engine`)

The judge also answers "are we done?": the goal (`metric`, `direction`, optional `target`) and stop (`max_rounds`, `dry_rounds`) make stopping an explicit decision, not a fixed loop count.
