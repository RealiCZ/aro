# a6ee3a9b isolated backport + full-chain certify — 2026-07-21

## Decision

**STOP at the full Salt conformance gate.** The one-line upstream backport is clean and all focused Algebra checks plus the Salt quick path-patch chain pass. The mandatory full Salt `cargo test` chain does not terminate on this host: after completing multiple package test binaries, the `salt` test binary remains CPU-active without additional test completions. This is a certify-infrastructure/resource blocker, not a candidate correctness failure.

Because Salt state-root and witness generation are consensus-critical downstream consumers, the full cross-repository gate is not waived. Decision-grade performance measurement, package/ship, integration, and PR creation were not started.

## Identity and isolation

- Upstream commit: `a6ee3a9b88058af37905dc462ce91ed2074a241c` (`remove useless modular operation (#982)`).
- Isolated baseline: `01b20e377460e7af9da069b0c96f2d1158a7b974`.
- Salt consumer pin: `19419f4d13e6c615b7a94cf3d2bf53d1052f723c`.
- Isolated branch: `experiment/a6ee3a9b-backport-certify-20260721`.
- Isolated worktree: `.aro-worktrees/a6ee3a9b-backport-certify-20260721`.
- Backport commit: `0fe47338d31c73e7d72b4a60b75951088485ca1a`.
- Application result: clean cherry-pick; one insertion and one deletion in `ff/src/fields/models/fp/montgomery_backend.rs`:
  - `r[i % N] = carry;`
  - becomes `r[i] = carry;`
- Loop invariant: `i` is in `0..N`, so the indexing expressions are equivalent.
- Original Algebra, Salt, and ARO production baselines were not modified.

## Protection checkpoint

The default `mega-putin` identity received HTTP 403 when pushing the experiment branch to `megaeth-labs/algebra`; no Algebra remote branch was created.

Before long validation, the exact `git format-patch` was base64-encoded and committed to the protected ARO evidence branch:

- ARO checkpoint: `7d3df331d07a6b0d4bd6dccccc9c603045653255`.
- Remote branch: `RealiCZ/aro:server/algebra-target`.
- Evidence: `docs/data/a6ee3a9b-backport-certify-20260721/`.

## Correctness results

| Gate | Result | Evidence |
|---|---|---|
| Root + curves fmt | PASS | both fmt commands exit 0 |
| `ark-ff`, `asm` | PASS | 52 + 42 tests; 0 failed |
| BLS12-381 + Bandersnatch curve tests | PASS | 137 + 53 tests; 0 failed |
| Salt Cargo path resolution | PASS | all five required packages resolve to the isolated candidate with `source=null` |
| Salt `check --all-targets` | PASS | dev-profile build completed |
| Salt quick path-patch tests | PASS | banderwagon 14/14; ipa-multipoint 18/18; doc tests clean/ignored as expected |
| Salt full path-patch chain | **BLOCKED** | full `cargo test` stalls in the `salt` test binary after earlier package suites pass |

The full run had already completed, among other suites:

- 14 tests, 0 failed;
- 18 tests, 0 failed;
- 195 tests, 0 failed, 2 ignored;
- two separate 2-test suites, 0 failed;
- 3 tests, 0 failed, 9 ignored.

It then entered another `salt` test binary and stopped producing completions while continuing to consume approximately 23 CPU cores.

## Resource/root-cause evidence

The first full run exposed seven pre-existing orphaned Salt test binaries with `PPID=1`:

- one had run for approximately 96 hours at about 1856% CPU;
- six ARO state-update/witness binaries had run for approximately 17.7–26.5 hours at about 293–357% CPU each.

Those stale orphaned test processes were terminated, and the full chain was restarted from a quiet host. On the clean rerun, the new `salt` binary still remained CPU-active at approximately 2326% CPU without new test completions for more than 18 minutes. This reproduces the same non-terminating resource pattern without cross-run contention.

This is not an OOM, compiler failure, assertion failure, or path-patch failure. It is a host/test-suite termination blocker. Replacing the required full command with a reduced suite or `--test-threads=1` would change the approved gate, so no such substitution was used.

## Performance and ship disposition

- Counterbalanced performance campaign: **not started** because the mandatory correctness gate did not complete.
- No performance benefit is claimed.
- No production configuration change.
- No integration into the Algebra baseline branch.
- No package, ship, or PR.
- No work on `#1046` subgroup-check.

## Raw artifacts

Run-local evidence is under:

`.aro-runs/a6ee3a9b-backport-certify-20260721/`

Key files:

- `backport.patch`
- `backport-commit.txt`
- `environment.txt`
- `certify-steps.tsv`
- `logs/ark-ff-tests-asm.log`
- `logs/bandersnatch-curve-tests-asm.log`
- `logs/salt-quick-path-patch.log`
- `logs/salt-full-path-patch.log`

The tracked evidence copy is under:

`docs/data/a6ee3a9b-backport-certify-20260721/`
