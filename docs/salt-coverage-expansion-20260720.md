# Salt ARO coverage expansion and epsilon B-class record — 2026-07-20

## Coverage audit

_Source: `.aro-runs/salt-coverage-audit-20260718.md`_

# Salt coverage audit and lane proposals — 2026-07-18

## Scope and production evidence

The current Salt ARO probes cover only two narrow paths at Salt `19419f4`:

| Existing lane | Timed/differential entrypoint | Transitively covered hot path | Demonstrated exclusions |
|---|---|---|---|
| `salt-msm` | `banderwagon::salt_committer::Committer::mul_index` | `calculate_prefetch_index` → x86 `add_affine_point` → `scalar_multi_asm` | `banderwagon/src/msm.rs`, generic `Element` serialization/hash/map APIs, `salt` crate |
| `salt-ipa` | `ipa_multipoint::CRS::commit_lagrange_poly` | `ipa::slow_vartime_multiscalar_mul` → `banderwagon::multi_scalar_mul` → arkworks MSM | `multiproof.rs`, proof create/verify, transcript, `multi_scalar_mul_par`, Salt state/trie |

Callgraph evidence: `probes/salt_msm_diff.rs:10-35`, `banderwagon/src/salt_committer.rs:161-188,203-282,393-433`; `probes/salt_ipa_diff.rs:10-23`, `ipa-multipoint/src/crs.rs:58-62,105-115,180-182`, `ipa-multipoint/src/ipa.rs:270-277`, `banderwagon/src/element.rs:415-425`.

Production relevance is established by the following currently mirrored MegaETH call sites:

| Workload | Production callers | Salt API sequence |
|---|---|---|
| Sequencer aggregation (priority 1) | `mega-reth/crates/megaeth/payload/src/aggregation/builder.rs:101-115,236-294` | `EphemeralSaltState::new_with_cache_read` → `update_plain` → `StateRoot::update` → `canonicalize` → `finalize` |
| Recovery / stateful execution | `mega-reth/crates/storage/provider/src/recovery/common.rs:860-880`; `stateless-validator/crates/stateless-core/src/executor.rs:553-584`; `mega-kona/crates/proof/executor/src/builder/core.rs:320-354` | `EphemeralSaltState::update_fin` + `StateRoot::update_fin` |
| Witness generation / verification | `mega-reth/bin/stateless/witness/src/generator/witness.rs:155-179`; `mega-reth/bin/replayer/src/replay/salt_witness.rs:150-160`; `stateless-validator/crates/stateless-core/src/executor.rs:482-490` | `Witness::create` → `Witness::verify` |
| Wire serialization | `mega-reth/crates/megaeth/rpc/src/witness_encoding.rs:25-45`; `stateless-validator/crates/stateless-common/src/witness_encoding.rs:53-94`; `mega-kona/bin/host/src/single/handler.rs:561-596` | legacy bincode encode/decode + zstd around `SaltWitness` |

`mega-evm` is not a direct Salt crate caller: its `SaltEnv`/compatible hasher interface is at `mega-evm/crates/mega-evm/src/external/test_utils.rs:260-279` and `external/hasher/mod.rs:1-68`. Therefore production relevance here is the sequencer/payload and stateless-execution ecosystem, not a nonexistent `mega-evm → salt` call edge.

## Public API coverage table

| Crate/API | Current coverage | Production relevance | Source evidence |
|---|---|---|---|
| `banderwagon::Committer::{new,mul_index}` | direct | precomputed commitments | `salt_committer.rs:161,204` |
| `banderwagon::multi_scalar_mul` | indirect via IPA commit | proof/commit MSM | `element.rs:415-425` |
| `banderwagon::MSMPrecompWnaf` | none | alternate parallel precompute API, no confirmed production caller | `msm.rs:17-61` |
| `ipa_multipoint::CRS::commit_lagrange_poly` | direct | commitment path | `crs.rs:180-182` |
| `MultiPoint::open` / `MultiPointProof::check` | none | proof create/verify | `multiproof.rs:70-167,269-321` |
| `ipa::{create,verify_multiexp,multi_scalar_mul_par}` | none | proof internals / verifier MSM | `ipa.rs:78-178,191-286` |
| `salt::EphemeralSaltState` / `StateRoot` | none | sequencer, recovery, stateless execution | `state/state.rs:179-850`; `trie/trie.rs:167-959` |
| `salt::Witness` / `SaltWitness` / `SaltProof` | none | witness/proof generator and validator | `proof/witness.rs:106-231`; `proof/salt_witness.rs:82-131`; `proof/prover.rs:216-247` |
| `salt-macros` scheduling macros | incidental only | parallelism in trie/proof lanes | `salt-macros/src/lib.rs:15-185` |

## New lane proposals: aligned three-tuples

| Proposed lane | Probe + differential oracle | Editable area | Rationale / gate boundary |
|---|---|---|---|
| `salt-state-update` **(build first)** | Construct deterministic cache-backed state batches; timed path is `EphemeralSaltState::update_plain` → `StateRoot::update` → `canonicalize` → `finalize`. Differential applies the identical seeded batches and fingerprints root plus canonical trie updates. | `salt/src/state/`, `salt/src/trie/`, `salt/src/cache/`; exclude DB/RPC callers and `salt-macros`. | Exact sequencer aggregation tuple; probe, differential and editable files are co-reachable. |
| `salt-witness` | Construct deterministic bucket lookups/updates; timed path is `Witness::create` then `verify`; differential fingerprints verified root and canonical witness serialization bytes. | `salt/src/proof/`, `ipa-multipoint/src/`, `banderwagon/src/element.rs`; exclude transport zstd/base64 wrapper and unexercised state mutation. | Covers operational generator/replayer and validator tuple, including cryptographic proof generation/verification. |
| `salt-multiproof-prove` | Deterministic `MultiPoint::open`; differential checks proof bytes and verifier result for seeded evaluations. | `ipa-multipoint/src/multiproof.rs`, `ipa.rs`, `transcript.rs`, `lagrange_basis.rs`, `banderwagon/src/element.rs`. | Direct proof-generation primitive; no edits outside differential reach. |
| `salt-multiproof-verify` | Precompute a deterministic valid multiproof once; timed path `MultiPointProof::check`; differential verifies result/root and rejects deterministic mutations. | `ipa-multipoint/src/multiproof.rs`, `ipa.rs`, `transcript.rs`, `banderwagon/src/element.rs`. | Direct verifier and `multi_scalar_mul_par` coverage. |
| `salt-witness-wire` **(deferred)** | `SaltWitness` bincode round-trip + compatibility vectors, including zstd/base64 only if made a Rust dependency fixture. | Restrict to `salt/src/proof/` serialization implementations actually called by the probe. | Current production wire wrapper lives in mega-reth/stateless-validator, not Salt. Do not create a Salt lane unless a Salt-owned serialization hotspot is established. |
| `salt-trie-rebuild` **(deferred)** | Deterministic populated state, `StateRoot::rebuild`, root+update differential. | `salt/src/trie/`, `salt/src/state/`, `salt/src/cache/`. | Valid distinct maintenance/recovery lane but secondary to incremental sequencer updates. |

## Build order

1. `salt-state-update` (highest production relevance, sequencer aggregation)
2. `salt-witness` (generator/validator coverage, including serialization fingerprint)
3. `salt-multiproof-prove`
4. `salt-multiproof-verify`

Each lane must pass: selfcheck → probe-lane calibrate → pipeline. `icount_epsilon_pct` is set to `0.02` (see `.aro-runs/salt-epsilon-bclass-20260718.md`). T52 supplies single-thread Rayon only for Ir measurement; no shell override is used.

## 2026-07-20 correction: first sequencer state-update lane

The backed-up `salt-state-update` draft based on `EphemeralSaltState::update_fin` plus `StateRoot::update_fin` is rejected: those convenience calls collapse update/canonicalize/finalize into the recovery shortcut and do not cover the sequencer aggregator's incremental hand-off. The replacement lane covers the aligned tuple `EphemeralSaltState::new(...).cache_read()` (standalone equivalent of mega-reth `new_with_cache_read`) → repeated `update` (the Salt core reached by `update_plain(..., true)`) with matching repeated `StateRoot::update` → separate `canonicalize` and canonical trie update → `StateRoot::finalize`. Its identical deterministic two-bucket workload makes canonicalization non-empty; the differential fingerprints ordered incremental, canonical, and merged state updates plus root and NodeId-sorted trie updates. Editable scope is restricted to the five co-reached state/trie implementation files named by the target.


## Ir epsilon decision

_Source: `.aro-runs/salt-epsilon-bclass-20260718.md`_

# Salt Ir epsilon B-class decision — 2026-07-18

**Decision:** set `icount_epsilon_pct: 0.02` for `salt-msm` and `salt-ipa`.

**Evidence:** stable production-profile Salt A/A observations available to this campaign were 0.0035% (salt-msm, T47), 0.0021% (salt-msm, T48), 0.0005% (salt-msm latest), and 0.00004945% (salt-ipa after T52 Rayon pin). The maximum applicable observed A/A spread is 0.0035%; the 3× policy minimum is 0.0105%. `0.02%` is the smallest simple two-decimal-percentage setting above that bound. The earlier 0.0128% reading was taken under a temporary non-production profile workaround and is not used as a production-lane floor.

**Effect:** acceptance and regression detection are symmetric at the tighter 0.02% Ir threshold. Small terminal `TERMINAL_CONFIRMED_WITH_TRADE` outcomes are expected and remain subject to normal policy.

**Scope:** B-class spec-only adjustment; no judge code changed. T52 automatically pins Rayon to one thread for Ir paths; wall-clock paths retain production parallelism.


## Existing-lane rescan

_Source: `.aro-runs/salt-epsilon-rescan-20260720.md`_

# Salt epsilon rescan B-class record — 2026-07-20

## Configuration

- `salt-msm.icount_epsilon_pct = 0.02`
- `salt-ipa.icount_epsilon_pct = 0.02`
- probe-lane rows: `original/v1/v2/v3/v4 × scale 1/8`
- both calibrations wrote 10 rows at `0.0200%`
- environment: `codspeed=4.18.3; cargo-codspeed=5.0.1; valgrind=3.26.0.codspeed5; rustc=1.96.0`

## Pipeline outcomes

| lane | frontier | attempted | accepted | natural stop |
|---|---|---:|---:|---|
| `salt-msm` | `add_affine_point` 94.7%, `mul_index` 4.4% | 2 | 0 | certify: zero reverify-pass survivors |
| `salt-ipa` | no actionable function survived frontier selection | 0 | 0 | certify: zero reverify-pass survivors |

The tighter Ir floor did not reveal an accepted regression or optimization in the former 0.02%–0.1% dead zone. `salt-msm` exhausted both actionable functions with `no-candidate`; `salt-ipa` had no actionable frontier entries. Exit code 2 for both pipelines is the expected work-order natural stop, not an execution failure.

## Evidence

- `.aro-runs/salt-msm-auto-20260720/events.jsonl`
- `.aro-runs/salt-msm-auto-20260720/manifest.json`
- `.aro-runs/salt-ipa-auto-20260720/events.jsonl`
- `.aro-runs/salt-ipa-auto-20260720/manifest.json`
- `memory/floors/salt-msm.json`
- `memory/floors/salt-ipa.json`


## Coverage expansion execution record

_Source: `.aro-runs/salt-coverage-expansion-bclass-20260720.md`_

# Salt coverage expansion B-class record — 2026-07-20

## `salt-state-update`

- Aligned sequencer path: cache-read state → repeated `update` / `StateRoot::update` → `canonicalize` → canonical trie update → `finalize`.
- Author verification: cargo check PASS; BENCH `65739979 ns_per_call`; repeated differential fingerprint stable at `ac10c1b35483fa0a1af7b48279ba662ea9b4d1bc00b75eca15e481ddffc074d9`.
- Selfcheck: PASS, Ir `32265295606 / 32265300617`, spread approximately `0.0000%`.
- Calibrate: 10 rows (`original/v1..v4 × 1/8`), all floors `0.0200%`.
- Pipeline: three functions attempted, zero accepted:
  - `add_affine_point` 24.4% — no-candidate
  - `shi_rehash` 14.5% — no-candidate
  - `value` 6.8% — no-candidate
- Natural stop: certify found zero reverify-pass survivors. Exit code 2 is expected work-order stop.
- Evidence: `.aro-runs/salt-state-update-auto-20260720/{events.jsonl,manifest.json,decision-tree.html}`.



## `salt-witness`

- Aligned proof path: deterministic persisted Salt state/trie → `Witness::create` → state-root extraction → `Witness::verify` → witness-backed reads → state/trie transition replay.
- Author verification: cargo check PASS; BENCH `117136062 ns_per_call`; repeated differential fingerprint stable at `ba072d14f7769902c5f9427b70bc48b220f1f629e4f3e243f0718f393d2522a9`.
- Selfcheck: PASS, Ir `34122654716 / 34122651247`, spread approximately `0.0000%`.
- Calibrate: 10 rows (`original/v1..v4 × 1/8`), all floors `0.0200%`.
- Pipeline frontier: four functions surfaced, zero accepted:
  - `mul_assign` 50.9% — out-of-scope-external/unlocated
  - `add_assign` 7.1% — no-candidate
  - `inverse` 3.7% — no-candidate
  - `base_to_scalar` 1.6% — no-candidate
- Certify limitation: with zero accepted/mergeable candidates, the final baseline `cargo test -p salt` still ran and exceeded the spec 1800-second timeout. This is recorded as a certify infrastructure timeout, not as a candidate correctness failure. No retry is justified because there is no candidate to certify.
- Evidence: `.aro-runs/salt-witness-auto-20260720/{events.jsonl,manifest.json,decision-tree.html}`.


## `salt-multiproof-prove` author verification

- Aligned proving path: deterministic default 256-point CRS + 16 seeded Lagrange polynomials + 32 ordered in-domain evaluation queries → `MultiPoint::open`; setup, commitments, evaluations, and owned timed query batches are outside the timed region.
- Baseline/detached worktree: Salt `19419f4d13e6c615b7a94cf3d2bf53d1052f723c` at `/nvme2/mega-engineer/workspace/salt-aro-multiproof-prove-verify`; ARO spec load PASS (`salt-multiproof-prove`).
- Toolchain: `rustc 1.96.0-nightly (bcf3d36c9 2026-03-19)`; target pins retain codspeed `4.18.3`, cargo-codspeed `5.0.1`, valgrind `3.26.0.codspeed5`, rustc `1.96.0`.
- Author build/check: `cargo check --release -p ipa-multipoint --example salt_multiproof_prove --example salt_multiproof_prove_diff` PASS.
- Formatting: `cargo fmt --all -- --check` PASS with both probes installed as detached-worktree examples.
- Measurement isolation: no `cargo`, `rustc`, `valgrind`, `cargo-codspeed`, or `codspeed` processes across four consecutive 30-second checks before execution.
- BENCH (one run, `ARO_BENCH_SCALE=1`): `BENCH 36859469 ns_per_call iters=1 scale=1`.
- Differential (two runs): both emitted `DIFF 0a7667c56f926414569cec48251e707e8e5aa1832414bf56c40f56002b4eae5a`.
- Differential semantics: fingerprints ordered canonical commitments, query points, claimed evaluations, canonical `MultiPointProof::to_bytes()` bytes, verifier success, and rejection of a deterministic first-query evaluation mutation; canonical proof decode/encode equality and valid verification are asserted.
- Lane policy: baseline pinned to `19419f4`; `icount_epsilon_pct: 0.02`; benchmark/terminal scales `[1, 8]`; exact current Salt CI conformance command set copied from the current B-class Salt target. Editable/probe coverage is restricted to the audited co-protected proof-generation files: `multiproof.rs`, `ipa.rs`, `transcript.rs`, `lagrange_basis.rs`, and `banderwagon/src/element.rs`.
- Per instruction, selfcheck, calibration, and pipeline were not run.


## `salt-multiproof-prove` pipeline

- Selfcheck: PASS, Ir `2213568039 / 2213569255`, spread approximately `0.0001%`.
- Calibrate: 10 rows (`original/v1..v4 × 1/8`), all floors `0.0200%`.
- Pipeline: `add_assign` at 9.1% self-time was attempted and returned `no-candidate`; zero accepted/mergeable edits.
- Natural stop: certify found zero reverify-pass survivors; exit code 2 is expected.
- Evidence: `.aro-runs/salt-multiproof-prove-auto-20260720/{events.jsonl,manifest.json,decision-tree.html}`.


## `salt-multiproof-verify` author verification

- Aligned verifier path: deterministic default 256-point CRS + 16 seeded Lagrange polynomials + 32 ordered in-domain public queries; canonical `MultiPointProof` generation/round-trip and public query construction occur before timing, then `MultiPointProof::check` runs repeatedly with a fresh transcript.
- Baseline/detached worktree: Salt `19419f4d13e6c615b7a94cf3d2bf53d1052f723c` at `/nvme2/mega-engineer/workspace/salt-aro-multiproof-verify-author`; ARO spec load PASS (`salt-multiproof-verify`, epsilon `0.02`).
- Toolchain: `rustc 1.96.0-nightly (bcf3d36c9 2026-03-19)`; target pins retain codspeed `4.18.3`, cargo-codspeed `5.0.1`, valgrind `3.26.0.codspeed5`, rustc `1.96.0`.
- Author formatting/build: `cargo fmt --all -- --check` PASS; `cargo check --release -p ipa-multipoint --example salt_multiproof_verify --example salt_multiproof_verify_diff` PASS.
- Measurement isolation: no `cargo`, `rustc`, `valgrind`, `cargo-codspeed`, or `codspeed` processes across four consecutive 30-second checks before execution.
- BENCH (one run, `ARO_BENCH_SCALE=1`): `BENCH 14254540 ns_per_call iters=1 scale=1`.
- Differential (two runs): both emitted `DIFF c030986bd56601d1dd34b06027123a8dd5b55ff5fd095336ac51d87ac8c483f2`.
- Differential semantics: fingerprints canonical precomputed proof bytes followed by ordered canonical commitment/point/result public-query bytes; asserts canonical proof round-trip and valid verification; rejects both a deterministic first-query result mutation and a deterministic canonically decodable final-proof-scalar mutation.
- Lane policy: baseline pinned to `19419f4`; `icount_epsilon_pct: 0.02`; benchmark/terminal scales `[1, 8]`; exact current Salt CI conformance command set copied from `salt-multiproof-prove`. `editable` equals `probe_covers` and is restricted to verifier/co-reached `multiproof.rs`, `ipa.rs`, `transcript.rs`, `lagrange_basis.rs`, and `banderwagon/src/element.rs`; proof-generation-only CRS/default-CRS and unrelated Salt paths are excluded.
- Per instruction, selfcheck, calibration, and pipeline were not run.


## `salt-multiproof-verify` pipeline

- Selfcheck: PASS, Ir `1719260475 / 1719261738`, spread approximately `0.0001%`.
- Calibrate: 10 rows (`original/v1..v4 × 1/8`), all floors `0.0200%`.
- Pipeline: `add_assign` at 10.8% self-time was attempted and returned `no-candidate`; zero accepted/mergeable edits.
- Natural stop: certify found zero reverify-pass survivors; exit code 2 is expected.
- Evidence: `.aro-runs/salt-multiproof-verify-auto-20260720/{events.jsonl,manifest.json,decision-tree.html}`.

## Final B-class stop

All requested existing-lane epsilon recalibration/rescan work and the four production-aligned new lanes completed their required gates. No pipeline produced an accepted or mergeable Salt source edit. The campaign therefore stops naturally with lane/spec/probe/floor/report artifacts retained in the ARO workspace. `salt-witness` alone ended with a final baseline test-suite timeout after its frontier had already produced zero candidates; this limitation does not hide an unverified candidate.
