# Plan workflow — free-form goal → validated 7-slot spec

Turn a plain-language goal into a validated `targets/<name>.json` (the 7 slots —
`spec-slots.md`), so a new repo is a new spec, not new code. This is a **first-class
executable entry**, not a manual wizard:

```sh
python3 -m aro plan "make the committer's scalar-mul faster" /path/to/repo
#   --name <id>     spec name (default <crate>-opt)
#   --crate <name>  which workspace member (required if >1)
#   --out <file>    where to write (default targets/<name>.json)
```

It mirrors autoresearch's `:plan` (free text → slots → dump), but its output is a spec
file and it **dry-runs build + probe + test + differential before writing** — a spec is
not trusted until it measurably works. `aro/plan.py` drives it.

## The flow (semi-automatic, with a human slot-dump gate)

1. **Detect** *(deterministic)* — `cargo metadata` → workspace crates and their
   `build`/`test` commands. Pick the crate (`--crate`, or the sole member).
2. **Fill** *(one agent call, `prompts/plan.md`)* — the agent reads the goal + the crate
   code, names the `hot_path` (file + fn), **writes the two probe files** (`probes/<name>.rs`
   microbench + `probes/<name>_diff.rs` differential — see `harness-protocol.md`), and emits
   the judgment slots (`metric`, `direction`, `sample_prefix`, `constraints`) as a JSON block.
3. **Assemble** *(deterministic)* — compose the 7-slot spec from detect + the agent's slots.
4. **Dry-run** *(deterministic)* — a throwaway worktree → `build` → run the probe (≥1
   sample? median?) → `test` (passing count = the `N_pre` sanity check) → run the differential
   probe (a fingerprint prints?). Each leg reported; never fabricated.
5. **SLOT DUMP** *(the human gate)* — print the 7 slots + probe paths + the dry-run results
   and a VERDICT (clean / incomplete). This is the checkpoint a human reviews before any run.
6. **Write** `targets/<name>.json`, and print the `aro run` command. (The human is the gate:
   review the dump, then launch.)

## What "valid" means (the dry-run must show)

- `build` exits 0.
- the microbench prints ≥1 `sample_prefix` sample (a median is computed) — the metric is
  genuinely isolable, not an end-to-end-only number.
- `test` exits 0; its passing count becomes the regression baseline `N_pre`.
- if a differential probe was written, it prints a `DIFF <hex>` fingerprint.

A dump whose VERDICT is "incomplete" means the probe/commands need fixing before running —
the spec is still written (so you can edit it), but flagged.

## Why a spec, not a Python class

The loop, judge, and generator never change per target. Adding a target is data
(`targets/*.json` + the two `probes/*.rs`), which is why generality costs no code and `aro
plan` can produce it end-to-end. For a repo you want optimized fully unattended with no spec
at all, see `autonomous-optimization.md`.
