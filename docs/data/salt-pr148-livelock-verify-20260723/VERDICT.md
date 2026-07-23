# Salt PR #148 — SHARED_COMMITTER natural-hang verification verdict

## Verdict

**PASS. On `dev-tko-node-1`, PR #148 eliminated the naturally occurring default-concurrency hangs reproduced at the pinned control `19419f4`.**

The control reproduced the issue in both requested configurations. The PR head completed every unrestricted repetition, both dedicated regression binaries, and the full ten-step conformance chain. No test command used a `--test-threads` override; only the isolated initialization-cost micro-measurement used `--test-threads=1` as declared in the plan.

## Fixed identities

- Control: `19419f4d13e6c615b7a94cf3d2bf53d1052f723c`
- PR #148 head: `ff8442f5413e6bf444af1b26f8f82b752db09475`
- Campaign: `salt-pr148-livelock-verify-20260723-v2`
- Plan SHA-256: `b5c910c7db19a4c60bab58a39a9481b7822cf829e2035a938c96c4158b6eefa1`
- Host: `dev-tko-node-1`

Both Salt worktrees were detached and clean before and after the campaign.

## A/B result

| Cohort / command | Terminated normally | Watchdog timeout | Observed hang rate |
|---|---:|---:|---:|
| Control ordinary | 2/5 | 3/5 | 60% |
| Control resize (`NUM_DATA_BUCKETS=2`, load factor 1%) | 2/5 | 3/5 | 60% |
| PR ordinary | 15/15 | 0/15 | 0% |
| PR resize (`NUM_DATA_BUCKETS=2`, load factor 1%) | 15/15 | 0/15 | 0% |

Successful PR wall times:

- ordinary: min `10.116s`, median `10.342s`, max `11.599s`;
- resize: min `10.061s`, median `10.235s`, max `10.353s`.

All six control timeouts fired at approximately 300 seconds. Each timeout log records the stuck test set, the process-group members, SIGTERM, SIGKILL where required, and an empty residual PID list. The six timeout logs are indexed by their immutable records in `run-records/` and bound by `manifest.json`.

## Dedicated regressions

- `shared_committer_init`: **50/50 PASS**, 0 timeout; median `0.548s`, p95 `0.598s`.
- `shared_committer_init_os_winner`: **50/50 PASS**, 0 timeout; median `0.546s`, p95 `0.600s`.

The first `shared_committer_init` invocation was an outlier at `16.850s`; the remaining distribution is represented by the median/p95 above and no invocation hung.

## Full conformance

All ten historical-equivalent entries passed with default libtest concurrency:

1. check all targets;
2. cargo-sort;
3. ordinary tests;
4. resize tests;
5. random stress;
6. RISC-V no-std check;
7. no-default-features tests;
8. no-default-features resize tests;
9. fmt;
10. clippy.

Result: **10/10 PASS**, 0 timeout.

## First-initialization wall cost

Twenty alternating control/PR pairs ran as fresh direct processes:

- control median: `0.400991s`;
- PR median: `0.402574s`;
- unpaired median delta: `+0.395%`;
- paired median relative delta: `+0.669%`;
- paired p95 relative delta: `+3.171%`;
- paired range: `-4.432%` to `+3.396%`.

This is below the predeclared 5% practical threshold. **No perceptible first-initialization regression was observed.** This is a descriptive host-local result, not a general performance claim.

## Cleanup and integrity

- Test/gate records: control 10, experiment 30, regressions 100, conformance 10, init-cost 40.
- Total immutable subprocess records including prebuild/metadata/invariant checks: 298.
- Unique trial keys: 298; unique run IDs: 298.
- Test watchdog timeouts: 6, all in the control cohort.
- Records with residual PIDs: 0.
- Post-campaign Cargo/Rust/Salt processes: 0.
- Both source worktrees remained clean and at the fixed SHAs.

The 18 metadata `fail` statuses in `summary.json` are the expected nonzero exits from `git symbolic-ref -q HEAD`; they are the detached-HEAD assertions for two checkouts across nine invariant snapshots, not test failures.

## Trust boundary

The campaign only fetched/read `megaeth-labs/salt`. It did not comment, approve, review, push a branch/tag/ref, or perform any other remote write to `megaeth-labs`.

No PR #148 response has been posted. Any future reply requires separate user approval.
