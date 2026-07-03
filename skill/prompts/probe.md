# Author an ISOLATION MICRO-BENCH for one hot function

You are writing a measurement probe, NOT an optimization. The function below is hot in
the parent workload but its wins are too small for the parent bench to resolve
(noise-limited). Your probe must measure THIS function tightly so a small real win
clears the noise floor.

Target function: `$fn` (defined in $files)
Crate/package: `$pkg`
Parent workload probe (for input realism — mirror its input distribution): $parent_probe

Write ONE Rust file to this exact absolute path (create it, whole file):

    $probe_path

Requirements (each is checked by a deterministic qualification gate — violating any
means the probe is rejected):

1. It is a cargo example for package `$pkg`: `fn main()` at top level, importing the
   target function from the crate under test.
2. It spends ≥ 60% of its self-time inside `$fn` (call it in a tight loop over
   realistic inputs; keep setup/IO outside the timed region).
3. Inputs are DETERMINISTIC (fixed-seed PRNG like xorshift — no system randomness)
   and realistic: mirror the sizes/distribution the parent workload feeds this
   function, not degenerate tiny inputs.
4. Scale-aware: read `ARO_BENCH_SCALE` (env, integer, default 1) and multiply the
   inner repeat count by it — same inputs, same path, just more repeats per sample.
5. Prints exactly one final line `BENCH s1 s2 s3 s4 s5` — five floating-point samples
   of ns per call (time a batch, divide by batch size). Consume an accumulator so the
   loop cannot be optimized away.
6. No new dependencies. No edits to any other file. Build it yourself with
   `cargo build --release -p $pkg --example $example` and fix compile errors before
   you finish.

You are in a THROWAWAY worktree of the target repo — build freely; only the probe
file at the absolute path above survives.

When done, reply with one line: `PROBE-READY <one-sentence description of the input
distribution you chose>`.
