# Draft: `test-bucket-resize` can livelock at high libtest concurrency during `SHARED_COMMITTER` initialization

> Draft only. Do not file without maintainer/user approval.

## Summary

The CI conformance command can nondeterministically livelock on a 32-logical-CPU x86_64 host when libtest uses its default concurrency:

```bash
NUM_DATA_BUCKETS=2 \
BUCKET_RESIZE_LOAD_FACTOR_PCT=1 \
cargo test --features test-bucket-resize
```

The same build terminates normally when bounded with either `--test-threads=4` or `--test-threads=16`. A clean Algebra baseline reproduces the default-thread hang, so this is independent of the investigated Algebra backport.

## Environment

- Host: x86_64 Linux, 32 logical CPUs
- Rust: `1.96.0-nightly (bcf3d36c9 2026-03-19)`
- Salt: `19419f4d13e6c615b7a94cf3d2bf53d1052f723c`
- GDB: 15.1
- Feature: `test-bucket-resize`
- Environment:
  - `NUM_DATA_BUCKETS=2`
  - `BUCKET_RESIZE_LOAD_FACTOR_PCT=1`

Independent comparison supplied by the reporter: the command is green in Salt CI; on a 15-core aarch64 host it completes in about 30 seconds and the affected tests complete in milliseconds.

## Reproduction

```bash
NUM_DATA_BUCKETS=2 \
BUCKET_RESIZE_LOAD_FACTOR_PCT=1 \
cargo test --features test-bucket-resize -- --nocapture
```

Observed behavior is scheduling-sensitive:

- one default-thread run completed: `188 passed; 0 failed; 2 ignored` in 6.74 s;
- another identical run made no progress beyond 20 s and remained in a stable wait/spin structure;
- a clean Algebra baseline hit the same 20 s timeout on its first short-window attempt.

## Thread-count matrix

| libtest concurrency | Result | Time |
|---:|---|---:|
| 4 | PASS: 188 passed, 2 ignored | 6.80 s |
| 16 | PASS: 188 passed, 2 ignored | 6.47 s |
| default on 32 logical CPUs | nondeterministic PASS or livelock | 6.74 s or no progress |

## GDB evidence

After confirming that the process had made no progress for 20 seconds, three samples were taken from the same inferior approximately 30 seconds apart using:

```gdb
info threads
thread apply all bt 12
continue
```

A separate full `thread apply all bt` sample was also captured before GDB 15.1 hit an internal recursive-symbol-printing failure.

Repeated sample counts:

Each path column below is the number of thread backtraces containing that path, not a raw string-occurrence count.

| Sample | Thread backtraces | atomic loads | Rayon `LockLatch` | futex waits |
|---|---:|---:|---:|---:|
| 1 | 65 | 42 | 14 | 15 |
| 2 | 65 | 42 | 14 | 15 |
| 3 | 65 | 47 | 14 | 15 |

The full-symbol sample resolves the acquire-load target to:

```text
salt::trie::trie::SHARED_COMMITTER + 8
```

Many test threads repeatedly execute atomic acquire loads or `_mm_pause`; other threads remain in Rayon latch/futex waits. The structure is unchanged across all three samples.

Relevant code:

```rust
// salt/src/trie/trie.rs
static SHARED_COMMITTER: Lazy<Arc<Committer>> = Lazy::new(|| {
    Arc::new(Committer::new(
        &CRS::default().G,
        platform::DEFAULT_PRECOMP_WINDOW_SIZE,
    ))
});
```

`Committer::new` performs parallel table construction with `bases.par_iter()` and calls `EdwardsProjective::normalize_batch` for each base (`banderwagon/src/salt_committer.rs`).

## Root cause hypothesis

Concurrent tests race on first access to an expensive process-global `spin::Lazy<Arc<Committer>>`. Losing threads busy-spin on the Lazy state word while the initializer enters Rayon parallel precomputation. At sufficiently high test concurrency this produces a stable initialization convoy/livelock involving the spin waiters and Rayon latch/futex waits.

This is not a test-size issue: bounding libtest concurrency to 4 or 16 makes the entire 188-test binary finish in about 7 seconds.

## Suggested next steps

1. Immediate CI/test-infra mitigation: bound this feature run with `--test-threads=4` (matching the effective CI concurrency reported by the investigator).
2. Root fix investigation:
   - avoid busy-spin waiting for this expensive global initialization;
   - initialize the committer before high-concurrency tests start, or use a parking wait primitive;
   - ensure the initializer does not depend on a Rayon scheduling configuration that can be starved by concurrent first-use callers.
3. Add a repeated high-concurrency regression test on a 32-thread x86_64 runner.
4. After a root fix, remove the bounded-thread mitigation and stress the default-thread command repeatedly.

## Attachments available

The investigation archive contains:

- one full-symbol GDB transcript;
- three stable 30-second-interval stack snapshots;
- thread-count run logs and result matrix;
- clean-baseline timeout log;
- environment/toolchain fingerprint;
- exact runner scripts and SHA-256 checksums.