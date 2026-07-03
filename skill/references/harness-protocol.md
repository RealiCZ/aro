# Harness protocol: the probe and the differential

The judge only measures what the harness exposes. Each target needs two Rust artifacts:
both the human/agent's job to get right, and the hardest, highest-leverage part of setting a
target up. Generation strategy lives in `optimization-lenses.md`; scoring in `judge-protocol.md`;
this doc is *only* the probe + the differential.

## The microbench probe (`probes/<name>.rs` → a cargo `example`)

Isolates the hot kernel so its cost is **measurable**, not diluted in an end-to-end number.

- Drives the chosen function behind the crate's **real public API**, in a tight loop.
- Prints ONE line `<PREFIX> <ns> <ns> ...`: per-call nanosecond samples. (The driver parses
  the leading numeric tokens after the prefix and stops at the first non-number, so trailing
  labels like `ns_per_call iters=...` are fine.)
- Fixed seed; `std::hint::black_box` on the inputs **and** the accumulator so the optimizer
  can't elide the work; a warmup pass before the timed region.
- A kernel that is most of an end-to-end number is still *diluted* there: only a direct
  microbench makes a sub-1% change resolvable above the A/A noise floor.
- **Honor `ARO_BENCH_SCALE` (the auto-tightening knob).** Read the env var `ARO_BENCH_SCALE`
  (default `1`) and multiply your batch / inner-repeat count by it:
  `let scale: u64 = std::env::var("ARO_BENCH_SCALE").ok().and_then(|s| s.parse().ok()).unwrap_or(1);`
  then `BATCH * scale`. At a tiny per-call cost, scheduler/frequency jitter dominates the A/A
  floor; when the judge sees a *noise-limited* result (a consistent directional effect, CI
  excludes 0, that the floor can't resolve), it re-benches at a higher scale so each sample
  averages more work and the floor drops, **without changing the path or the inputs**. A probe
  that ignores this var simply can't be auto-tightened (the judge detects the floor not dropping
  and stops, reporting an honest `noise-limited`).
- **The sample is a TIME per operation, never a count.** The judge parses the leading numbers
  as the scored metric and minimizes them (unless `direction` says otherwise). A probe that
  prints a throughput or iteration COUNT under `minimize` scores BACKWARDS: fewer ops looks
  like a win and slowdowns get accepted. The plan dry-run's polarity leg catches the common
  case mechanically (a count grows with `ARO_BENCH_SCALE`; a per-op time does not), but get
  it right at authoring time.
- **Implement SPIN MODE (required for profiling).** When `argv[1]` parses as an integer,
  run the SAME workload in a continuous loop until that many seconds elapse, then print one
  `SPUN <n>` line (informal, not parsed) instead of the BENCH line:

  ```rust
  if let Some(secs) = std::env::args().nth(1).and_then(|s| s.parse::<u64>().ok()) {
      let deadline = std::time::Instant::now() + std::time::Duration::from_secs(secs);
      let mut n = 0u64;
      while std::time::Instant::now() < deadline { run_batch(&mut acc); n += 1; }
      println!("SPUN {}", n);
      return;
  }
  ```

  The profiler launches the probe with a seconds argument and samples the RUNNING process
  to build the frontier map. It retries fixed-iteration probes at higher `ARO_BENCH_SCALE`
  values as a fallback, but that is best-effort: a probe without spin mode can exit before
  the sampler attaches, and the map comes out empty.

## The differential probe (`probes/<name>_diff.rs`)

The byte-identical behaviour guarantee: what matters for crypto / consensus / EVM, beyond
the test suite.

- Feed many deterministic pseudo-random inputs (fixed seed; a tiny inline PRNG, no new deps)
  through the **same** function in BOTH the baseline and candidate worktrees.
- Fold every output into one fingerprint (FNV-1a / xor) and print `<PREFIX> <hex>`.
- The judge requires the baseline and candidate fingerprints to be **identical**, else the
  change is invalid regardless of speed.

### Adversarial coverage is mandatory when a change rests on an invariant

A happy-path-only differential rubber-stamps an unproven assumption. When the optimization is
safe *only because* some invariant holds (e.g. "only one dimension can change on this path"),
the corpus MUST exercise exactly the paths that argument depends on: push the state the change
claims "can't change here" **to and over** its limit, hit the error / exceptional arms, vary
nesting and ordering, not just the common case.

This is what lets the generator safely **adopt** a high-leverage, invariant-guarded elimination
(`optimization-lenses.md`) instead of retreating to a trivial change: the judge can only be a
safety net if the differential would actually catch a wrong assumption. Behaviour is pinned by
the differential **plus** an in-code `debug_assert!`, never by editing `tests/` (the guard
rejects that; the candidate touches implementation source only).

## Why a probe, not formal verification

The probe + a sound statistical judge give an *empirical* byte-identical guarantee over a large
random corpus, cheaply, for any language with a cargo `example` target. It is only as strong as
the corpus, so make the corpus adversarial.
