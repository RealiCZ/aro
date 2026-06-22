You are setting up a performance-optimization target. Your cwd is a DISPOSABLE throwaway worktree of the target repo — build and verify freely; you do NOT need to clean up and you must NOT worry about preserving the working tree (it is discarded after you finish). Your job is to turn the free-form goal into a measurable contract: name the hot path, WRITE two probe files, and emit the judgment slots. You do NOT optimize anything here.

Goal: $goal
Worktree (your cwd): $repo
Crate to optimize: `$crate`  (its directory, relative to your cwd: `$crate_dir`)
Workspace crates:
$crates

## Do this

1. **Find the hot path.** Read the crate's source (and any existing `benches/` for clues) and identify the single function the goal is really about — the one where the time goes. Profile-guess from the code; the harness will confirm it's hot in a dry-run. Note its file (path relative to the repo root) and function name.

2. **Write the microbench probe** to this absolute path: `$probe_path`
   A cargo `example` for `$crate` that drives that function behind its REAL public API in a tight loop and prints ONE line `$prefix <ns> <ns> ...` — per-call nanosecond samples. Fixed seed; `std::hint::black_box` on inputs AND the accumulator; a warmup pass before the timed region. **Make it scale-aware**: read `ARO_BENCH_SCALE` (env, default 1) and multiply the batch by it, so the judge can auto-tighten a noise-limited result without changing the path/inputs. (See the harness protocol.)

3. **Write the differential probe** to this absolute path: `$diff_path`
   A cargo `example` for `$crate` that feeds many deterministic pseudo-random inputs (fixed seed; a tiny inline PRNG — no new deps) through the SAME function, folds every output into one FNV-1a/xor fingerprint, and prints exactly `DIFF <hex>`. This is the byte-identical behaviour check. (If the function genuinely has no inputs to vary, skip this file and say so.)

4. **Verify both build and print** before finishing: copy each into `$crate_dir/examples/` (that exact path — `$crate` is the package name, NOT necessarily a directory), `cargo run --release -p $crate --example <name>` (FOREGROUND), and confirm the `$prefix`/`DIFF` line appears. No cleanup needed — this worktree is thrown away; never run a repo-wide `git clean`. The canonical probes live at the absolute paths above.

## Then emit ONLY this JSON block (no prose after it)

```json
{
  "hot_path": { "file": "<crate>/src/<file>.rs", "fn": "<hot_fn>" },
  "metric": "ns_per_call",
  "direction": "minimize",
  "sample_prefix": "$prefix",
  "constraints": {
    "editable": ["<the file(s) a fix may touch>"],
    "no_new_deps": true,
    "byte_identical": true,
    "notes": "<anything the optimizer must NOT change — a public API, a tuning constant — or empty>"
  }
}
```

Rules: edit only the probe files you were told to write; never touch `Cargo.toml`/`Cargo.lock`, `benches/`, or `tests/` in the target. `direction` is `minimize` unless the goal is to raise a throughput-style number.
