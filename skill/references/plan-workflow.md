# Plan workflow — add a new target

Turn a plain-language goal into a validated `targets/<name>.json` TargetSpec, so a
new repo is a new spec (not new code). This is the agent-driven wizard; the slots
it fills are documented in `spec-slots.md`. It mirrors autoresearch's `:plan`, but
its output is a spec file and its **dry-run actually runs build + probe + test** on
the target repo — a spec is not accepted until it measurably works.

## Trigger

- "add a target / set up ARO on `<repo>`", "plan an aro run", "what should the metric be"
- A repo with no `targets/*.json` yet.

## The flow (ask one thing at a time; ≤6 questions total)

### 1. Capture the goal
`AskUserQuestion`: what to make faster/smaller, and the stop condition — `metric`,
`direction` (minimize/maximize), optional `target` value (null = open-ended, run
until `dry_rounds`).

### 2. Analyze the repo
Detect, don't ask, where you can: the crate/package (`Cargo.toml` → `name`); the
test command (`cargo test --release -p <pkg>`); candidate hot files (grep the
domain terms, or read the profiler if a binary exists). Propose; let the user correct.

### 3. Scope = editable regions + context anchors
`regions`: which files the generator may edit (must resolve to ≥1 real file).
`context.file` + `context.anchors` (`[["struct","X"],["fn","y"]]`): the code put in
front of the generator. Prefer the hot file from step 2.

### 4. Metric + probe (the critical step)
The metric MUST be isolable behind a microbench. If one already prints
`<PREFIX> <ns...>`, point at it. **If not, write one** — a `probes/<name>.rs` that
isolates the highest-leverage operation and prints `<sample_prefix> <ns...>` on one
line (a kernel diluted in an end-to-end number can't be cleanly optimized). Fill
`bench.{probe,pkg,example,sample_prefix,metric}` and `profile.{example,spin_secs,sample_secs}`.

### 5. Build / test commands
`build` and `test` as token lists (e.g. `["cargo","test","--release","-p","<pkg>"]`).
`test` is the correctness gate and the source of the regression baseline `N_pre`.

### 6. Dry-run — MANDATORY gate (this is what makes the spec real)
Before writing the spec, actually run, on a throwaway worktree / the repo:
1. `build` → must exit 0.
2. drop the probe in as a cargo example and run it → must print ≥1 `sample_prefix` sample.
3. `test` → must exit 0; record the passing count as the `N_pre` sanity check.

```
Dry-run result:
  build:  exit {0/err}
  probe:  {N} samples on '<prefix>'  (e.g. 2546 2549 ...)
  test:   exit {0/err}, {N_pre} passing
  Status: ✓ VALID / ✗ INVALID — {reason}
```

If any step fails, fix the probe/commands and re-run. **Do not write a spec that
hasn't passed its dry-run.**

### 7. Write + confirm
Write `targets/<name>.json` (all slots from `spec-slots.md`: + `goal`, `stop`,
`prompts`, `regions`, `read_phase`, `blind`). Show it, then offer to launch:
`python3 -m aro run targets/<name>.json`.

## Critical gates (refuse otherwise)

- Metric must be isolable behind a probe that prints `<prefix> <float...>` — not a subjective or end-to-end-only number.
- The probe dry-run must yield ≥1 sample; `build` and `test` must exit 0.
- `regions` must resolve to ≥1 file; `context.file` must exist.
- Never fabricate the probe's numbers — run it. A spec that hasn't measurably run is not written.

## Why a spec, not a Python class

The loop, judge, and generator never change per target. Adding a target is data
(`targets/*.json` + maybe one `probes/*.rs`), which is why generality costs no code
and a wizard can produce it end-to-end.
