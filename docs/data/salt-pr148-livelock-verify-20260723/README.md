# Salt PR #148 livelock verification evidence

This directory is the fail-closed runner and evidence store for control
`19419f4d13e6c615b7a94cf3d2bf53d1052f723c` and PR #148
`ff8442f5413e6bf444af1b26f8f82b752db09475` on `dev-tko-node-1`.
Preparing, describing, refreshing, or self-testing this directory does **not**
start the campaign. Only `python3 salt_pr148_runner.py --run` can do so.

## Fixed identity

- ARO worktree: `/nvme2/mega-engineer/workspace/aro-salt-livelock-verify`
- Control: `/nvme2/mega-engineer/workspace/salt-pr148-control`, detached at
  `19419f4d13e6c615b7a94cf3d2bf53d1052f723c`, tree
  `31dc0405c080ad366bdf1f99531e8f5d0d8b493c`
- PR: `/nvme2/mega-engineer/workspace/salt-pr148-experiment`, detached at
  `ff8442f5413e6bf444af1b26f8f82b752db09475`, tree
  `c1d82feaa3b54e17cf5abfa710bcf0405500577b`
- Origin: `https://github.com/megaeth-labs/salt.git`
- Both `Cargo.lock` files:
  `539e8ecdfda09b7267b5a6104fe368b804f113dd171b7451808d077113c8e1a9`
- Build targets: `.aro-runs/salt-pr148-verify/{control,pr}` under the ARO
  worktree, never under either Salt checkout.

Before the first campaign command and after every phase, the runner verifies the
exact resolved paths, detached HEADs, origin URL, empty tracked-and-untracked
status, tree IDs, and `Cargo.lock` hashes. A source write or identity change
fails closed.

## Process and environment guarantees

Every subprocess, including metadata capture, uses an argv array,
`shell=False`, a fresh session/process group, and one finally-based lifecycle.
The runner records leader/process elapsed immediately when the leader finishes;
cleanup elapsed is separate. Timeout, interruption, launch/wait/evidence error,
or normal leader exit with descendants triggers whole-group TERM, grace, KILL,
and reap/absence checks. `/proc/<pid>/stat` supplies process-group and ancestry
identity; a supplemental scan matches resolved paths by path components rather
than string prefixes. Residuals are removed before the next trial and any
remaining residual fails closed.

Before explicit per-command values are applied, ambient build/test controls are
removed: all `CARGO_*` (including `CARGO_HOME`, encoded flags, config and jobs),
`RUST_TEST_THREADS`, `RUSTUP_TOOLCHAIN`, `RUSTFLAGS`, `NUM_DATA_BUCKETS`,
`BUCKET_RESIZE_LOAD_FACTOR_PCT`, `RANDOM_*`, `RAYON_*`, `TOKIO_*`, make/job
concurrency, and credential/secret variables. Records contain stripped variable
**names**, explicit non-secret values, and a SHA-256 safe-environment
fingerprint; secret values are never evidence fields.

Each deterministic `trial_key` has one immutable atomic JSON record. Duplicate
keys are rejected. Attempt records are persisted even for Popen, wait, log-hash,
or normal store-index failures. Resume skips exact completed control records
(including expected timeout); fail-closed PR/prebuild/conformance/init-cost
trials skip only exact passing records. A prior failed PR gate aborts. There is
no duplicate-rerun mode. `state.json` has stable campaign and plan identities.

All evidence writes occur while `.campaign.lock` is held during CLI operations.
`--describe-plan` is lock-safe. `--refresh-manifest` updates only
`manifest.json`; it preserves state, summary, and report.

## Exact command plan

`campaign-plan.json` is authoritative and records every argv, explicit env,
count, timeout, SHA, and command/environment identity hash. Cargo commands use
`--locked` wherever Cargo accepts it; `cargo sort` and `cargo fmt` do not.

Prebuild uses `--no-run --message-format=json` for control/PR ordinary and
resize, plus dated-nightly PR no-default and no-default-resize. This keeps the
300-second runtime watchdog focused on execution rather than first compilation.
Compile/check/clippy watchdogs are 900 seconds.

Control performs five ordinary and five resize trials and records every clean
completion, including timeout. PR performs exactly 15 ordinary plus 15 resize
trials, fail closed. Resize explicitly sets:

```text
NUM_DATA_BUCKETS=2 BUCKET_RESIZE_LOAD_FACTOR_PCT=1
```

Regressions run these exact binaries 50 times each:

```text
cargo test --locked -p salt --test shared_committer_init -- --nocapture
cargo test --locked -p salt --test shared_committer_init_os_winner -- --nocapture
```

Conformance reproduces the ten historical
`targets/salt-multiproof-prove.json` entries once each, in this exact order:

1. `cargo check --locked --all-targets`
2. `cargo sort --check --workspace --grouped --order package,workspace,lints,profile,bin,benches,dependencies,dev-dependencies,features`
3. `cargo test --locked`
4. resize test with the explicit resize environment
5. exact random-stress filter/environment, with only historical
   `--test-threads=4` removed and `--locked` added
6. `cargo +nightly-2026-03-20 check --locked -p salt --target riscv64imac-unknown-none-elf --no-default-features`
7. `cargo +nightly-2026-03-20 test --locked --no-default-features`
8. the same dated nightly no-default test with `--features test-bucket-resize`
9. `cargo fmt --all -- --check`
10. `cargo clippy --locked --all-targets -- -D warnings`

No conformance command has a test-thread override.

## Init-cost measurement

Executable discovery and `--list` are stable `init-discovery` records outside
the measured `init-cost` group. The measured group is exactly 20 alternating
control/PR pairs. Each fresh direct process runs the exact named baseline with
`--exact --test-threads=1`. Statistics use process elapsed (never cleanup),
report paired relative deltas, control-first/PR-first strata, descriptive
min/median/p95/max, and the predeclared 5% practical threshold. The report is
descriptive and never emits a generic `performance_claim_allowed` boolean.

## Exact gates

Gates compare exact trial-key sets and exact command/env identity hashes:

- experiment: exactly 15 ordinary + 15 resize;
- regressions: exactly 50 for each exact binary name;
- conformance: exactly the ten ordered entries once each.

Extra, duplicate, missing, mislabeled, failed, or identity-mismatched records do
not pass. Before any subprocess observations, `no_process_residuals` is `null`,
not affirmative.

## Safe commands (do not run Cargo)

```text
python3 salt_pr148_runner.py --describe-plan
python3 selftest_runner.py
python3 -m py_compile salt_pr148_runner.py selftest_runner.py
ruff check salt_pr148_runner.py selftest_runner.py
python3 salt_pr148_runner.py --refresh-manifest
```

Selftests cover ambient contamination and secret redaction, hostile argv,
timeout and normal-exit orphan cleanup, interruption/Popen attempted records,
duplicate keys, partial resume policies, exact gates/identities, canonical
conformance order/toolchains, checkout contamination, manifest-only refresh,
process-vs-cleanup timing, paired init statistics, lock-safe state, and null
zero-run residual gates. They never invoke Cargo or authorize `--run`.

Do not commit, push, review, comment, or otherwise write to
`megaeth-labs/salt` as part of this runner.
