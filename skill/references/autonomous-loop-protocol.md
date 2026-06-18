# Autonomous loop protocol

The phases of one round, the artifacts each produces, and the rules each obeys. Driver: `aro/engine.py:run_backtest`.

## Pre-loop (once)

- **Freeze the baseline.** Resolve `baseline_ref` to a commit SHA; every worktree is created from it, so candidates never touch the user's checkout and "the baseline" is byte-stable.
- **Build + bench the baseline.** Establishes the starting metric(s).
- **Regression baseline (`N_pre`).** Run the test suite once on the baseline and record its passing-test count; later candidates must keep it (the absolute regression gate).
- **Resume / cross-run compound.** Rebuild the cumulative accepted patch from memory (`pareto.txt` + `patches/`) and apply it to the baseline (`baseline_resumed`), so re-running into the same `--out` continues from the *advanced* baseline, not scratch.
- **A/A calibration.** Bench the (advanced) baseline against *itself* `aa_runs` times; the per-metric noise floor = 90th percentile of |Δ%|, clamped ≥0.5%. This is the bar a real gain must clear. (`aro/eval.py:calibrate_floors`)

## Per round

1. **Observe** — `target.compute_region_hint(baseline)` profiles the baseline (`aro/profile.py`, macOS `sample`), ranks the heaviest in-binary functions, and attaches the relevant code (spec `context` anchors). Output: the region hint.
2. **Read** *(if `read_phase`)* — `generator.understand(ctx)`: a READ-ONLY `claude` call (`prompts/read.md`) that returns a concrete plan — which computation to change, why byte-identical, what data-layout. Decouples deriving from implementing. Output: `ctx.plan`.
3. **Generate** — `generator.propose(ctx)`: the agentic write-compile-fix loop (`prompts/agentic.md`) in a throwaway worktree (`claude --dangerously-skip-permissions`), **seeded with the accepted patch so it edits and diffs against the current advanced baseline** (else a 2nd-round edit to the same file can't apply on top of the 1st): edit → `cargo build`/`test` → fix → iterate until it builds and tests pass. ARO takes the git diff as whole-file edits. The agent self-stops; only a high hang-guard caps it.
4. **Judge** — `aro/eval.py:evaluate`: gate 0 guard → gate 1 correctness (apply on a fresh baseline worktree → build → test → differential) → gate 2 significance (paired, order-alternated A/B vs the A/A floor + bootstrap CI). See `judge-protocol.md`.
5. **Record + compound** — write the outcome to memory; if accepted, fold the patch into the working baseline (rebuild, re-bench, refresh the hint) so the next round optimizes on top of it.
6. **Check goal / stop** — if `goal.target` is met → stop (`goal_met`); if `dry_rounds` consecutive non-accepts → stop (`diminishing_returns`); else continue until `max_rounds`.

## Robustness

Every fallible step is guarded; a non-recoverable failure returns a *partial* report rather than crashing. A candidate that fails any gate is recorded with its verdict and the loop continues. The agent's own build/test is best-effort grounding; the judge re-verifies independently (maker-checker).
