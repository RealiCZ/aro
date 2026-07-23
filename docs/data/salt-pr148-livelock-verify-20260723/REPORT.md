# Salt PR #148 livelock verification report

Generated: `2026-07-23T03:11:32.073716Z`

Attempted subprocess records: **298**

## Gates

- `conformance_all_passed`: **true**
- `control_natural_hang_reproduced`: **true**
- `experiment_all_passed`: **true**
- `no_process_residuals`: **true**
- `pr148_validation_passed`: **true**
- `regressions_all_passed`: **true**

## Exact groups

- `conformance/cargo-sort`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/check`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/clippy`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/fmt`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/no-default-features-resize-test`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/no-default-features-test`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/no-std-check`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/random-stress`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/test`: 1/1 pass; 0 timeout; 0 other failure
- `conformance/test-bucket-resize`: 1/1 pass; 0 timeout; 0 other failure
- `control/ordinary`: 2/5 pass; 3 timeout; 0 other failure
- `control/resize`: 2/5 pass; 3 timeout; 0 other failure
- `experiment/ordinary`: 15/15 pass; 0 timeout; 0 other failure
- `experiment/resize`: 15/15 pass; 0 timeout; 0 other failure
- `init-cost/control`: 20/20 pass; 0 timeout; 0 other failure
- `init-cost/pr`: 20/20 pass; 0 timeout; 0 other failure
- `init-discovery/control`: 1/1 pass; 0 timeout; 0 other failure
- `init-discovery/pr`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/cargo`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/checkout-control-branch`: 0/9 pass; 0 timeout; 9 other failure
- `metadata/checkout-control-head`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/checkout-control-origin`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/checkout-control-status`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/checkout-control-tree`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/checkout-pr-branch`: 0/9 pass; 0 timeout; 9 other failure
- `metadata/checkout-pr-head`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/checkout-pr-origin`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/checkout-pr-status`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/checkout-pr-tree`: 9/9 pass; 0 timeout; 0 other failure
- `metadata/control-to-pr-diff`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/free`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/gh`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/git`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/lscpu`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/pr-view`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/rustc`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/rustup`: 1/1 pass; 0 timeout; 0 other failure
- `metadata/uname`: 1/1 pass; 0 timeout; 0 other failure
- `prebuild/no-default`: 1/1 pass; 0 timeout; 0 other failure
- `prebuild/no-default-resize`: 1/1 pass; 0 timeout; 0 other failure
- `prebuild/ordinary`: 2/2 pass; 0 timeout; 0 other failure
- `prebuild/resize`: 2/2 pass; 0 timeout; 0 other failure
- `regressions/shared_committer_init`: 50/50 pass; 0 timeout; 0 other failure
- `regressions/shared_committer_init_os_winner`: 50/50 pass; 0 timeout; 0 other failure

## State

```json
{
  "active_phase": null,
  "campaign_id": "salt-pr148-livelock-verify-20260723-v2",
  "completed_phases": [
    "metadata",
    "prebuild",
    "control",
    "experiment",
    "regressions",
    "conformance",
    "init-cost",
    "finalize"
  ],
  "created_utc": "2026-07-23T02:25:03.505078Z",
  "failed_phases": {},
  "invocation": 1,
  "plan_sha256": "b5c910c7db19a4c60bab58a39a9481b7822cf829e2035a938c96c4158b6eefa1",
  "schema_version": 2,
  "updated_utc": "2026-07-23T03:11:32.029794Z"
}
```

## Init cost

Paired descriptive statistics use process elapsed only, stratify alternating order, and compare against the predeclared 5% practical threshold. They never emit a generic performance-claim authorization.
