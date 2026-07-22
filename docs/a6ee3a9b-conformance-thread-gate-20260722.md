# a6ee3a9b B-class conformance gate decision: Salt resize thread bound

Date: 2026-07-22
Status: user-approved gate variant; implementation checkpointed before performance measurement

## Decision

The full Salt `test-bucket-resize` conformance command is run with the libtest argument:

```text
-- --test-threads=4
```

This is environment alignment, not test reduction: Salt CI runs the same full command on 4-core runners. The command still selects the complete `test-bucket-resize` suite with the same feature flags and environment.

## Root cause and evidence

The unbounded command can livelock on a 32-logical-CPU host during first access to the process-global `SHARED_COMMITTER: spin::Lazy<Arc<Committer>>`. Waiting test threads busy-spin on the Lazy state while the initializer dispatches Rayon work. Three GDB snapshots approximately 30 seconds apart showed no forward progress. The clean Algebra baseline reproduced the timeout, while the same binary completed with `--test-threads=4` and `--test-threads=16`.

References:

- `docs/a6ee3a9b-salt-livelock-investigation-20260722.md`
- `docs/salt-test-infra-livelock-issue-146.md`
- https://github.com/megaeth-labs/salt/issues/146

## Scope judgment

The bound is applied to every full default-feature `test-bucket-resize` conformance command:

- `scripts/validate-salt-path-patch.sh` full chain in the Algebra candidate tree;
- `targets/salt-ipa.json`;
- `targets/salt-multiproof-prove.json`;
- `targets/salt-multiproof-verify.json`;
- `targets/salt-state-update.json`;
- `targets/salt-witness.json`.

Two sibling forms remain unchanged:

1. `no-default-features-resize-test`: `--no-default-features` disables Salt's `parallel` feature. `salt-macros::iter!` uses sequential `.iter()` and `num_threads!()` returns 1, so the observed Lazy-plus-Rayon livelock mechanism is absent.
2. `random-stress`: this command selects one named ignored test, so libtest does not create a competing set of concurrent first-use callers. Its explicit Rayon behavior and workload remain unchanged.

## Static regression proof

A dedicated verifier was run before and after the change:

- RED: exit 1; all five target entries and the full-chain script were reported unbounded.
- GREEN: exit 0; every default-feature full resize command is bounded, while the two excluded sibling forms remain unchanged.

Evidence is retained under `.aro-runs/a6ee3a9b-pr-ready-20260722/` and will be copied into the final PR-ready evidence package.

## Remaining execution chain

1. Re-run affected conformance on the a6ee3a9b candidate until all green.
2. Only then unfreeze Ir and decision-grade counterbalanced wall-clock measurement.
3. Produce and push the PR-ready evidence package; do not open any MegaETH PR.
