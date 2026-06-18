# Autonomous optimization (no pre-written spec)

The unattended mode: you have a repo and **no** pre-written spec or probe — the
agent itself locates the hot path, writes the probe, optimizes, and verifies. This
is the path a blind run actually walked end-to-end. (The spec-driven mode,
`targets/*.json` + `python3 -m aro run`, is for when a human has already isolated
the metric.)

**Why the discipline below is non-negotiable.** A blind agent, given only this
protocol and zero answer hints, did everything right — profiled, isolated, wrote
probes, proved byte-identical — and then **confidently shipped a −53% regression**.
The only thing that caught it was a *sound* measurement. An agent's optimization
intuition is unreliable; the judge is the moat. So:

## The protocol (run it end to end)

0. **Read `memory/lessons.jsonl` first** — past dead ends and regressions across
   all prior runs. Don't burn a round re-deriving a known −53% trap.
1. **Explore** — what crates exist, what they do.
2. **Profile — don't guess.** Build a hot-loop release example, sample it with
   macOS `/usr/bin/sample <pid> <secs>` (no sudo) or `aro/profile.py:top_functions`.
   Find the heaviest *in-binary compute* function. Never optimize "looks slow".
3. **Pick** a high-leverage target function.
4. **Isolate it into a microbench probe** (`examples/*.rs`, auto-discovered, no
   Cargo.toml change) that prints `<PREFIX> <ns...>`. A kernel diluted in an
   end-to-end number can't be measured — this step is the one a human usually does,
   and the hardest to get right.
5. **Implement ONE behaviour-preserving change.**
6. **Verify — the part that decides truth** (reuse `aro/{eval,stats,guard,target}.py`
   or replicate them):
   - frozen-baseline git worktrees;
   - **per-worktree `CARGO_TARGET_DIR`** — a shared one makes cargo reuse the first
     worktree's build, so baseline and candidate bench the SAME binary and every Δ
     collapses to ≈0 (this silently hid both a −53% regression and a +14% win);
   - `build` + `test` (keep the baseline's passing-test count — no silent drop);
   - **recompile self-check** — a changed candidate that built without a `Compiling`
     line reused a stale binary → measurement-unsound, reject;
   - **random-input differential** — deterministic-seed probe, fingerprint every
     output, require byte-identical baseline vs candidate, else the change is invalid;
   - **A/A floor + paired (order-alternated) A/B + bootstrap CI** — improved only if
     `Δ% < -floor` AND the CI excludes 0.
7. **Record the outcome to `memory/lessons.jsonl`** — win OR loss, with the WHY —
   via `aro/lessons.py:append`, so the next run (any target) doesn't repeat it.

## Honest stop

If nothing clears the floor, say so plainly. A within-noise or regressed result,
recorded with its reason, is a valid and useful outcome — and far better than a
laundered "looks faster" that ships a regression.

## What the agent must NOT do

Optimize on a hunch without profiling; bench without per-worktree isolation; trust
its own "this is faster" over the judge; touch `benches/`/`tests/`/`Cargo.*`; or
report a verdict the statistics don't support.
