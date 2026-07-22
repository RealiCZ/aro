# a6ee3a9b B-class conformance gate decision: bound every Salt test call

Date: 2026-07-22
Status: user-approved extended gate; checkpoint required before the final conformance run

## Decision

Every Salt test invocation in the conformance chain uses the libtest argument:

```text
-- --test-threads=4
```

This includes ordinary `cargo test`, feature variants, filtered stress tests, and `--no-default-features` variants. It is environment alignment, not test reduction: Salt CI executes with the equivalent four-core test concurrency, while each command retains its full package, feature, filter, ignored-test, and environment selection.

The gate must be deterministic. Repeating an unbounded command until scheduling happens to avoid the race is not an acceptable correctness gate.

## Root cause and evidence

On the 32-logical-CPU host, concurrent first access to process-global `SHARED_COMMITTER: spin::Lazy<Arc<Committer>>` can livelock. Waiters busy-spin on the Lazy state while the initializer dispatches Rayon work. Three GDB snapshots approximately 30 seconds apart showed no forward progress. The clean Algebra baseline reproduced the timeout. The same complete test binary terminated with `--test-threads=4` and `--test-threads=16`.

Attempt 1 of the post-decision full chain also entered the same non-progress state during ordinary `cargo test`, before the `test-bucket-resize` command. This proves that the scheduling risk is not confined to that feature command and is the direct reason for extending the bound to every Salt test call.

References:

- `docs/a6ee3a9b-salt-livelock-investigation-20260722.md`
- `docs/salt-test-infra-livelock-issue-146.md`
- https://github.com/megaeth-labs/salt/issues/146

Issue #146 is already open and was verified by live readback. Attempt 1 is archived locally but has not been added as a comment; further issue communication requires separate user approval.

## Scope

The bound is applied to all Salt test calls in:

- `scripts/validate-salt-path-patch.sh`, including quick and full paths;
- `targets/salt-ipa.json`;
- `targets/salt-msm.json`;
- `targets/salt-multiproof-prove.json`;
- `targets/salt-multiproof-verify.json`;
- `targets/salt-state-update.json`;
- `targets/salt-witness.json`.

The static verifier enumerates all Salt-backed target specs instead of maintaining a hand-selected allowlist. It currently checks 23 target conformance test commands and six path-patch script forms, including ordinary, resize, filtered random-stress, and no-default-features variants.

## Runner argument-order correction

Adding libtest arguments exposed a pre-existing helper limitation: the old `run_cargo` appended `--manifest-path` after all caller arguments, so a caller containing the harness separator `--` sent the manifest option to the test binary. The helper now extracts the Cargo subcommand and invokes:

```text
cargo <patches> <subcommand> --manifest-path <Salt/Cargo.toml> <remaining arguments>
```

This keeps `--manifest-path` in Cargo's argument domain and `--test-threads=4` in libtest's argument domain. Two targeted RED/GREEN checks captured and fixed the incorrect argument order.

## Static regression proof

- Initial RED: exit 1; the original default-feature resize commands were unbounded.
- Initial GREEN: exit 0 after the narrow gate.
- Extended-scope RED: exit 1; 18 remaining target commands and five remaining script forms were correctly reported unbounded.
- Extended-scope GREEN: exit 0; all 23 Salt target conformance test commands and all quick/full script test calls are bounded.
- Manifest-order RED/GREEN: verified the Cargo subcommand → manifest → harness ordering described above.

Evidence is retained under `.aro-runs/a6ee3a9b-pr-ready-20260722/` for inclusion in the PR-ready package.

## Attempt disposition

- Attempt 1: ordinary `cargo test` livelock; intentionally terminated, cleaned, and archived.
- `proc_9c9910949eff`: ordinary tests passed, then the resize command exited 101 because the old helper placed `--manifest-path` after the harness separator. This was an infrastructure argument-order failure, not a conformance verdict.
- No attempt is treated as green by retry luck. The final conformance run uses the fully extended deterministic gate.

## Remaining execution chain

1. Checkpoint and push this extended gate before the long run.
2. Run the full conformance chain once with the deterministic bound; require all green.
3. On green, unfreeze Field/MSM Ir A/B and decision-grade Salt E2E counterbalanced wall-clock measurement.
4. Produce and push the PR-ready evidence package; do not open any MegaETH PR.
