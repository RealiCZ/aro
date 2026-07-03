# aro sweep: the frontier-map meta-loop

`python3 -m aro sweep <spec.json>` profiles a workload, ranks the hot functions, buckets
each by OWNER and by what the cross-run lessons already recorded, and emits a **frontier
map**: where the time goes, what is our lever vs untouchable, what has been tried (and the
judge's verdict), and the **actionable frontier**: the untried in-crate functions, heaviest
first. Code: `aro/sweep.py`.

## Why a sweep (and why it terminates)

autoresearch explores an *open* space (a better model always exists) and never converges.
ARO, correctness-constrained, explores a *closed* space per target (a fixed function has a
finite set of byte-identical speedups), so it **converges**. The hot-function set is finite,
so the sweep covers it once and stops at a map. "Continuous exploration" lives one level up:
re-deploy the sweep across workloads / crates / relaxed constraints over time.

## Maturity ladder: L1 → L2 → L3

A self-running loop earns trust in stages; don't jump to unattended before the
report-only and assisted stages are trusted on a target.

| level | what it does | command | human role |
|---|---|---|---|
| **L1 report-only** | profile → map; no changes | `aro sweep <spec>` · `find_hotpath.py` | reads the map |
| **L2 assisted** | propose ONE change, judged | `aro run <spec>` · `aro plan` | reviews + merges the diff |
| **L3 unattended** | walk the frontier, judge each, compound | `aro sweep <spec> --attempt` | reviews the batch of accepts |

The judge is identical at every level; what changes is how much runs without a
human in the inner loop. **`accepted` is never "merged"**: L2 and L3 both hand a
proven (correctness + speed) diff to a human, who owns the architecture call.

## The buckets

Each ranked frame ≥ `--min-pct` (default 1.5%) is classified:
- **not-ours**: the symbol belongs to crypto (keccak/sha3/…) or a runtime/external crate
  (revm/alloy/hashbrown/foldhash/…). Recorded with its % as "not our lever", skipped.
- **ours, untried**: an in-crate function (the target crate's token appears in the symbol)
  with no recorded lesson → **the actionable frontier**.
- **ours, attempted**: a lesson already records a verdict for it (within-noise / noise-limited
  / accepted). Not re-queued blindly.
- **ours, architecture-gated**: a lesson flags a structural / maintainability / reviewer
  objection. Needs a human call (`accepted` ≠ should-merge); the sweep only surfaces it.

## How it composes (the loop)

The map is the deterministic skeleton. The per-function OPTIMIZATION is the existing
per-target loop, which the map *surfaces and orders*:

```
aro sweep  → frontier map → pick the heaviest untried in-crate fn
           → aro run / the autonomous protocol on it  (agent proposes, judge verifies)
           → accepted? fold into the baseline (a new baseline_ref)
           → re-run aro sweep  → re-profiled on top of the win (compounding); the ranking shifts
           → repeat until the untried bucket is empty → converged
```

An accepted change moves the baseline, so re-sweeping re-profiles on top of it: the same
compounding the per-run loop does, at the frontier level.

## `--attempt`: the L3 automation of that loop

`python3 -m aro sweep <spec> --attempt [--max-attempts N] [--rounds-per-fn N]
[--out-dir DIR] [--out map.md]` runs the box above unattended. It writes **no new
judging code**: it orchestrates the existing `run_backtest` (the full per-target
judge) and `profile_ranked`:

1. Profile → bucket → take the heaviest **untried** in-crate function.
2. Locate its source file (scoped grep for `fn <name>` in the target crate; a name
   that can't be located (a fully-inlined generic leaf, a macro-generated fn) is
   skipped and recorded as `unlocated`).
3. Derive a per-function spec (region guard + read-phase `context` locked to that
   file) and run `run_backtest` on it: the same A/A floor + paired A/B +
   differential + auto-tighten.
4. **All attempts share one `--out-dir`**, so `run_backtest`'s resume re-applies the
   cumulative accepted patch each call: cross-function compounding for free.
5. On an accept, re-profile **on top of** the accepted edits and re-bucket (only on
   accept: a non-accept leaves the ranking unchanged, so just take the next).
6. Stop when the untried bucket empties or `--max-attempts` is hit. Each attempt is
   also recorded as a cross-run lesson, so a later sweep auto-dedups it.

It is **loop-ready** by construction: the four primitives a self-running loop needs:

- **budget**: `--max-attempts` caps the fan-out; `bench_scales` bounds re-benching.
- **run-log**: every attempt and every candidate verdict streams to `events.jsonl`.
- **gate**: architecture-gated functions are surfaced, never auto-touched; a human
  owns the merge call.
- **denylist**: the per-function region guard locks edits to the located file;
  `Cargo.toml`/lock, `benches/`, `tests/` stay off-limits.

**Comprehension debt.** N unattended accepts leave N diffs a human still has to
understand before merging. The attempt map lists exactly those diffs (with their Δ
and files) so the debt is visible, not hidden. Cost is **overnight-scale** (a full
`run_backtest` per function); run it as the foreground, harness-tracked process:
**never a backgrounded subagent** (a subagent that backgrounds a build can't be woken).

## Requirements

- The spec's `benchmark_probe` / `profile.example` must be **spin-capable** (run long enough to
  be sampled: it's profiled the same way as `find_hotpath.py`, via macOS `sample`). A debug
  build helps symbolication (the release profile strips debuginfo; build with
  `CARGO_PROFILE_RELEASE_DEBUG=2 CARGO_PROFILE_RELEASE_STRIP=false` for a clean profile).
- Owner classification reads the crate token from the (mangled) symbol; the target crate is
  taken from `benchmark_probe.pkg`.

## Honest bounds (so "exhaustive" isn't a lie)

- **Exhaustive over *this* profile, not all inputs.** A different / broader workload exposes
  different hot paths; re-run the sweep on it. The map names which workload it profiled.
- **"No change found" ≠ "no change exists."** The untried→attempted transition is whatever the
  agent's search produced this time, not an optimality proof. More budget / a stronger agent
  may find more.
- **Cost.** A full attempt-each pass is overnight-scale (per-worktree rebuilds per candidate).
  The map itself is cheap (one profile); the attempts are not.
- **Resolution ceiling (`--attempt`).** The judge measures the **whole-workload** bench, not
  an F-isolated microbench: improving a 2%-of-profile function by 30% moves the workload
  metric by ~0.6%, often below even the auto-tightened floor, so it comes back
  `noise-limited`. `--attempt` goes heaviest-first, spending budget where resolution is best;
  the small-fraction tail degrades to an honest `noise-limited` (it needs an isolation probe
  via `aro plan`, or a workload that stresses it), it is not silently dropped.
- **Architecture-gated items need a human.** The map flags them; it does not resolve the
  "is it worth it" tradeoff: that dimension is outside the judge.
