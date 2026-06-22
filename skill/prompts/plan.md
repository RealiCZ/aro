You are setting up a performance-optimization target. You are in the target repo (your cwd). Your job is to turn the free-form goal into a measurable contract: name the hot path, WRITE two probe files, and emit the judgment slots. You do NOT optimize anything here.

Goal: $goal
Repo: $repo
Crate to optimize: `$crate`
Workspace crates:
$crates

## Do this

1. **Find the hot path.** Read the crate's source (and any existing `benches/` for clues) and identify the single function the goal is really about — the one where the time goes. Profile-guess from the code; the harness will confirm it's hot in a dry-run. Note its file (path relative to the repo root) and function name.

2. **Write the microbench probe** to this absolute path: `$probe_path`
   A cargo `example` for `$crate` that drives that function behind its REAL public API in a tight loop and prints ONE line `$prefix <ns> <ns> ...` — per-call nanosecond samples. Fixed seed; `std::hint::black_box` on inputs AND the accumulator; a warmup pass before the timed region. (See the harness protocol: isolate the kernel so a sub-1% change is resolvable.)

3. **Write the differential probe** to this absolute path: `$diff_path`
   A cargo `example` for `$crate` that feeds many deterministic pseudo-random inputs (fixed seed; a tiny inline PRNG — no new deps) through the SAME function, folds every output into one FNV-1a/xor fingerprint, and prints exactly `DIFF <hex>`. This is the byte-identical behaviour check. (If the function genuinely has no inputs to vary, skip this file and say so.)

4. **Verify both build and print** before finishing: copy each into `$crate/examples/`, `cargo run --release -p $crate --example <name>` (FOREGROUND), confirm the `$prefix`/`DIFF` line, then DELETE the temp copies and `git restore . && git clean -fdq` so the repo is left pristine. The canonical probes live at the absolute paths above, not in the repo.

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
