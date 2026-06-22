# aro sweep — the frontier-map meta-loop

`python3 -m aro sweep <spec.json>` profiles a workload, ranks the hot functions, buckets
each by OWNER and by what the cross-run lessons already recorded, and emits a **frontier
map**: where the time goes, what is our lever vs untouchable, what has been tried (and the
judge's verdict), and the **actionable frontier** — the untried in-crate functions, heaviest
first. Code: `aro/sweep.py`.

## Why a sweep (and why it terminates)

autoresearch explores an *open* space (a better model always exists) and never converges.
ARO, correctness-constrained, explores a *closed* space per target (a fixed function has a
finite set of byte-identical speedups) — so it **converges**. The hot-function set is finite,
so the sweep covers it once and stops at a map. "Continuous exploration" lives one level up:
re-deploy the sweep across workloads / crates / relaxed constraints over time.

## The buckets

Each ranked frame ≥ `--min-pct` (default 1.5%) is classified:
- **not-ours** — the symbol belongs to crypto (keccak/sha3/…) or a runtime/external crate
  (revm/alloy/hashbrown/foldhash/…). Recorded with its % as "not our lever", skipped.
- **ours, untried** — an in-crate function (the target crate's token appears in the symbol)
  with no recorded lesson → **the actionable frontier**.
- **ours, attempted** — a lesson already records a verdict for it (within-noise / noise-limited
  / accepted). Not re-queued blindly.
- **ours, architecture-gated** — a lesson flags a structural / maintainability / reviewer
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

An accepted change moves the baseline, so re-sweeping re-profiles on top of it — the same
compounding the per-run loop does, at the frontier level.

## Requirements

- The spec's `benchmark_probe` / `profile.example` must be **spin-capable** (run long enough to
  be sampled — it's profiled the same way as `find_hotpath.py`, via macOS `sample`). A debug
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
- **Architecture-gated items need a human.** The map flags them; it does not resolve the
  "is it worth it" tradeoff — that dimension is outside the judge.
