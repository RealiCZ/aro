# Lane 1 REX6 SSTORE/LOG pre-spec aligned-probe evidence

Date: 2026-07-22

- ARO source: `/nvme2/mega-engineer/workspace/aro`, branch `server/mega-evm-rex6`
- ARO HEAD: `4563916f9938432bba2eac2b267ed8cb2c871215`
- mega-evm baseline: `996c16a91d071e3bb95780ea7dc5d4f1677bf746`
- Workload identity/version: `mega-evm-rex6-sstore-log` / `3`
- Scope: Lane 1 probe pair, contract test, validator, and reproduction evidence only. No target spec, call trace, mutation, epsilon, floors, or pipeline artifact was added.

## Review-blocker TDD: RED then GREEN

The contract in `tests/selftest_rex6_lane_artifacts.py::case_69` was strengthened before production artifact changes. It requires a single Keccak-256 digest over canonical bytes, a 64-lowercase-hex `DIFF`, a pre-measurement zero-scale guard, validator locking/staging/atomic publication, verified cleanup without `ignore_errors=True`, source hashes, and the expanded manifest.

Focused RED command:

```text
python3 -c 'from tests.selftest_rex6_lane_artifacts import case_69; case_69()'
```

Observed before implementation (exit 1):

```text
File ".../tests/selftest_rex6_lane_artifacts.py", line 180, in case_69
  assert 'println!("DIFF {digest:x}");' in diff_main
AssertionError
```

Focused GREEN reran the same command and produced:

```text
case_69 OK: aligned REX6 SSTORE/LOG pre-spec probe contracts
```

## Canonical differential fingerprint

The differential now appends the same explicit fields in deterministic order to one byte vector. Variable-size fields and collection counts use an unsigned 64-bit big-endian length prefix. Returned state remains address-sorted and storage remains slot-sorted; no Rust `Debug` representation is used. `alloy_primitives::keccak256` hashes the completed canonical serialization exactly once. No dependency was added.

Current complete output (including one terminating newline in each raw file):

```text
DIFF 6f26a41c0c58774723597fb0e1e58c07bb7e8bf5b3087b3f8aa293a10c00ec21
```

`diff-run1.stdout` and `diff-run2.stdout` are byte-identical; each is 70 bytes.

## Scale-zero contract

The timed probe rejects `ARO_BENCH_SCALE=0` before warmup, measurement, or division with the deterministic message:

```text
ARO_BENCH_SCALE must be greater than zero
```

The real injected-example validator invokes `SpecTarget._cargo_run(..., scale=0)`, requires this exact failure text, and records `"zero_scale_rejected": true`. Invalid scale zero therefore cannot produce `NaN` samples.

## Serialized, staged, atomic validation

Invocation:

```text
export PATH="$HOME/.cargo/bin:$HOME/.foundry/bin:$PATH"
cd /nvme2/mega-engineer/workspace/aro
python3 docs/data/mega-evm-rex6-lanes-20260722/sstore-log/validate.py
```

The validator:

1. takes an exclusive `fcntl.flock` under `.aro-runs/locks`;
2. creates a unique run staging directory under `.aro-runs/staging`;
3. creates and initializes a unique detached baseline worktree;
4. exercises real `SpecTarget.write_probe`, `_cargo_run`, `bench`, and `run_diff_probe` APIs;
5. verifies scale-zero rejection, BENCH parsing, 64-hex DIFF parsing, and two byte-identical complete DIFF outputs;
6. force-removes any registered worktree, removes a residual path, prunes after removal, removes `target._td_root`, and verifies path, registration, and target directory absence;
7. writes every generated output in staging and calls `os.replace` only after validation and cleanup succeed, replacing `SHA256SUMS` last while still holding the lock.

No fixed shared output is deleted before validation. Failure leaves the prior final evidence untouched.

Real validator result (exit 0):

```text
BENCH 458450 455840 448450 433680 429891
ARO parser samples: [351813.0, 345202.0, 336703.0, 335042.0, 366082.0]
DIFF 6f26a41c0c58774723597fb0e1e58c07bb7e8bf5b3087b3f8aa293a10c00ec21
diff full stdout bytes: 70
diff byte-identical: true
zero scale rejected: true
target tracked status: ""
worktree cleaned: true
target dir cleaned: true
```

`validation.json` binds this result to the ARO HEAD, baseline, and current probe sources:

```text
probes/mega_evm_rex6_sstore_log.rs      186b4738e7114ddacc0c0cda64edbbf715ed9df45a6e7981c4aa1e62d1c51b17
probes/mega_evm_rex6_sstore_log_diff.rs ad8fb141b4f58e8c5a82e6540b644fed9f0d4d150f1c2e2ba4c7ed9ecf796aec
```

## Intentional failure cleanup proof

A deterministic failure was injected after worktree setup and target-directory creation:

```text
LANE1_RUN_TOKEN=cleanup-test-20260722 \
LANE1_INJECT_FAILURE=after-worktree-setup \
python3 docs/data/mega-evm-rex6-lanes-20260722/sstore-log/validate.py
```

Observed exit 1 with the original `RuntimeError: injected validator failure after worktree setup`. Postconditions were asserted by shell checks:

```text
FINAL_OUTPUTS_UNCHANGED=true
WORKTREE_PATH_ABSENT=true
WORKTREE_UNREGISTERED=true
TARGET_DIR_ABSENT=true
STAGING_ABSENT=true
```

The checked paths were the disposable worktree `/nvme2/mega-engineer/workspace/rex6-lane1-baseline-disposable-cleanup-test-20260722` and target root `/home/mega-engineer/workspace/.aro-rex6-lane1-validator-cleanup-test-20260722-td`. Hashes of all five pre-existing final outputs were identical before and after the failed run.

## Manifest and final checks

`SHA256SUMS` contains `validate.py`, both probe paths, BENCH output, both DIFF outputs, and `validation.json`. It is directly verifiable from the evidence directory:

```text
cd docs/data/mega-evm-rex6-lanes-20260722/sstore-log
sha256sum -c SHA256SUMS
```

Observed all seven entries `OK` (exit 0).

Additional commands and results:

```text
python3 -c 'from tests.selftest_rex6_lane_artifacts import case_69; case_69()'
=> case_69 OK; exit 0

rustup run nightly-2026-04-14 rustfmt --edition 2021 --check \
  probes/mega_evm_rex6_sstore_log.rs \
  probes/mega_evm_rex6_sstore_log_diff.rs
=> exit 0

python3 selftest.py
=> SELFTEST PASSED (67 case groups, including case_69); exit 0

cmp docs/data/mega-evm-rex6-lanes-20260722/sstore-log/diff-run1.stdout \
    docs/data/mega-evm-rex6-lanes-20260722/sstore-log/diff-run2.stdout
=> exit 0
```

Final repository checks found empty tracked status, only the intended base worktree registration, and no validator/probe/Cargo process. The ten intended Lane 1 artifacts remain uncommitted and no remote write, commit, or push was performed.
