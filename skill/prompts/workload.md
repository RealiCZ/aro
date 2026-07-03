# Author a NEW WORKLOAD VARIANT (bench probe + differential oracle)

You are expanding COVERAGE, not optimizing. The current workload's hot-function
frontier is exhausted; your job is a new DETERMINISTIC workload variant that
exercises DIFFERENT code paths in the same crate, so new functions become hot.

Crate/package: `$pkg`
Current workload probe (read it — your variant must differ in INPUT DISTRIBUTION,
not in which APIs it calls; v1 scope: vary sizes/mixes/values only): $parent_probe
Functions already covered (your variant should make OTHER in-crate functions hot):
$covered_fns

Write TWO Rust files (cargo examples for `$pkg`), whole files, to these exact paths:

1. `$probe_path` — the bench probe:
   - deterministic inputs (fixed-seed xorshift), a DIFFERENT distribution than the
     parent (e.g. larger/smaller sizes, skewed mixes, boundary-heavy values);
   - scale-aware: multiply inner repeats by env `ARO_BENCH_SCALE` (int, default 1);
   - prints one final line `BENCH s1 s2 s3 s4 s5` (five ns-per-op samples; consume
     an accumulator so the loop is not optimized away).

2. `$diff_path` — the differential oracle for THIS workload:
   - deterministic pseudo-random cases (fixed seed, >= 64 cases) over the same
     input distribution;
   - folds every output into one fingerprint and prints exactly one line
     `DIFF <16-hex>`;
   - SENSITIVE: any behaviour change in the exercised functions must change the
     fingerprint (fold full outputs, not just lengths or sums of a few bits).

Both are checked by deterministic gates: same-seed determinism, a seeded-mutation
alarm test on the oracle (a mutated crate MUST change your `DIFF` line), and a
coverage-increment profile (your workload must surface at least one uncovered
in-crate function). No new dependencies; no edits to any other file. Build both
with `cargo build --release -p $pkg --example <name>` and fix errors before
finishing.

You are in a THROWAWAY worktree; only the two files at the absolute paths above
survive. When done reply one line: `WORKLOAD-READY <one-sentence description of
the distribution and which functions you expect to become hot>`.
