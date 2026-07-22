# mega-evm REX6 lane coverage proposal

Date: 2026-07-22
Status: proposal only — do not create lanes before user approval
Baseline: `996c16a91d071e3bb95780ea7dc5d4f1677bf746`

## Conclusion

The current ARO lane has no runtime coverage of REX6. `sweep_hotloop_v2.rs` fixes its timed workload at `MegaSpecId::REX4`, while `evm_semantics_diff.rs` stops at REX5. The current broad `editable`/`probe_covers` scope of `crates/mega-evm/src` is therefore not justified as an aligned intersection of paths reached by both a timed probe and a seeded semantic oracle.

This proposal applies the Salt coverage discipline: an eligible lane must use the same production VM entry, exact workload tuple, initial state, spec, and repetition policy on both sides; the semantic fingerprint must be deterministic; and `editable` must be the verified intersection reached by both sides, not a directory-level aspiration.

## Current gap matrix

| Surface | Timed probe | Differential | Exact tuple |
|---|---|---|---|
| `evm/instructions.rs` REX6 metering and `create_rex6` | No | No | No |
| `evm/host.rs::inspect_storage` | Partial under REX4 | Partial through unrelated cases | No |
| REX6 limit trackers | No REX6 branches | No REX6 branches | No |
| `external/gas.rs` scaled SALT path | Minimal-capacity path only | Partial | No |
| System-exempt unscaled gas path | No | No | No |
| PR #330 VM workloads | Criterion only | No matching oracle | No |

The present timed tuple is REX4 with 96 reused-EVM units of `16×ADD → SSTORE+SLOAD → LOG2(32B) → CALL`. The current differential uses fresh EVMs for 200 seeded storage cases plus independent scenarios across `MINI_REX/REX/REX3/REX4/REX5`. Their overlap is thematic, not tuple-exact.

## Ranked aligned triples

### 1. REX6 SSTORE/LOG state-transition lane

**Timed probe/workload**

Use `MegaEvm::transact(MegaTransaction)` with the exact builders behind:

- `sstore_heavy/rex6/sstore_100`
- `sstore_heavy/rex6/sload_100`
- `sstore_heavy/rex6/sstore_sload_100`

Add fixed variants for zero→nonzero, nonzero→nonzero, reset-to-original, and `LOG0/LOG1/LOG2` with fixed data lengths. Both timed and differential sides must share the same DB, bytecode, transaction tuple, and repetitions.

**Differential/fingerprint**

Hash stable encodings of halt/success class, gas used, output, ordered logs, state sorted by address and slot, and sorted `DynamicGasCost::get_bucket_ids()` when using a crowded SALT environment. Never hash `Debug` output or raw map iteration.

**Provisional editable intersection**

- `crates/mega-evm/src/evm/instructions.rs`
- `crates/mega-evm/src/evm/host.rs`
- `crates/mega-evm/src/external/gas.rs`
- `crates/mega-evm/src/limit/{limit,frame_limit,compute_gas,data_size,kv_update,state_growth}.rs`

Implementation must remove any file not confirmed by both runtime call traces. Expected production path: `Mega::run → MegaEvm::transact → additional_limit_ext::sstore → HostExt::inspect_storage → storage_gas_ext::sstore → DynamicGasCost::sstore_set_gas → raw SSTORE → record_storage_compute_gas! → AdditionalLimit::record_compute_gas/on_sstore → trackers`.

### 2. REX6 CREATE/CREATE2 single-window metering lane

**Timed probe/workload**

Reuse the exact semantics of:

- `create_deploy/rex6/create_10`
- `create_deploy/rex6/create2_10`
- `make_create_bytecode(10)`
- `make_create2_bytecode(10)`

Use a fresh DB/EVM per workload and fixed net-new and pre-funded-no-code address variants with identical nonce, salt, and initcode on both sides.

**Differential/fingerprint**

Encode halt/success, gas, created addresses in creation order, caller nonce, and each created account's nonce, balance, code hash, canonical code bytes, and sorted final state. A static-frame CREATE2 boundary case is eligible only if present on both sides.

**Provisional editable intersection**

`evm/instructions.rs`, the CREATE-reached portions of `evm/host.rs` and `external/gas.rs`, and the verified `limit/` tracker files. Expected production path includes `forward_gas_ext::{create,create2} → storage_gas_ext::create → create_rex6 → compute_created_address → DynamicGasCost::create_contract_gas → raw create → record_storage_compute_gas!`, plus `AdditionalLimit::before_frame_init`.

### 3. REX6 SELFDESTRUCT beneficiary lane

**Timed probe/workload**

Use the production VM entry, not a direct host helper. Add two exact REX6 variants derived from `bench_selfdestruct`:

1. funded source → empty distinct beneficiary;
2. funded source → existing distinct beneficiary.

The current benchmark only registers `equivalence/rex2/rex4/rex5`, so it is not existing REX6 evidence.

**Differential/fingerprint**

Encode halt/success, gas, source and beneficiary balances/nonces/existence/code hashes, observable destruction state, and sorted storage. Protect tracker semantics through externally visible state and boundary accept/halt results; do not add a public tracker-inspection API solely for the lane.

**Provisional editable intersection**

The verified SELFDESTRUCT-reached parts of `evm/instructions.rs`, `evm/host.rs`, `external/gas.rs`, and `limit/{limit,frame_limit,compute_gas,data_size,kv_update,state_growth}.rs`.

### 4. REX6 applied EIP-7702 authority lane

**Timed probe/workload**

Run type-4 authorization lists through `MegaEvm::transact(MegaTransaction)`, derived from `bench_eip7702_authlist` and `make_recovered_auth_list`, with fixed applied-net-new, applied-existing, chain-ID mismatch, nonce mismatch, and rejected-code variants. Current rows are REX5-only.

**Differential/fingerprint**

Preserve input order. Encode applied/skipped effects via authority nonce, delegation code bytes/hash, balance, caller nonce, halt/error class, gas, and authority state sorted by address. Protect limits with boundary transactions, not a new public usage API.

**Provisional editable intersection**

- `crates/mega-evm/src/evm/execution.rs`
- verified portions of `evm/host.rs` and `external/gas.rs`
- `limit/{limit,frame_limit,data_size,kv_update,state_growth}.rs`

Expected path: `record_rex6_eip7702_authority_accounting → scan_applied_eip7702_authorizations → AdditionalLimit::on_rex6_eip7702_authority_applied`; net-new authorities additionally reach `DynamicGasCost::new_account_gas`.

### 5. REX6 system-origin exemption / unscaled SALT lane

**Timed probe/workload**

Use a real system-originated `MegaTransaction`; do not call `mark_exempt` directly. Run fixed zero→nonzero SSTORE, value CALL to an empty account, CREATE, and oracle/beneficiary volatile-access variants in both minimum-capacity and crowded SALT environments. Include the same ordinary-caller tuple as a sensitivity control.

**Differential/fingerprint**

For system-originated variants, both capacity environments must match in halt/success, gas, state, created addresses, balances, storage, and logs. Ordinary callers should retain capacity-sensitive gas. Sort bucket IDs; the exempt path should encode an empty set if it performs no bucket lookup.

**Provisional editable intersection**

- `crates/mega-evm/src/evm/context.rs`
- verified portions of `evm/instructions.rs`, `evm/host.rs`, and `external/gas.rs`
- `limit/{limit,frame_limit,compute_gas,data_size,kv_update,state_growth}.rs`

Expected production gate: `MegaContext::on_new_tx → is_system_originated → AdditionalLimit::mark_exempt`, followed by host gas helpers selecting `DynamicGasCost::*_gas_unscaled` for `LimitCheck::Exempt`.

## PR #330 benchmark material

At the pinned baseline, `benches/common/mod.rs::SPEC_IDS` includes REX6, so reusable workload prototypes include:

- `sstore_heavy/rex6/{sstore_100,sload_100,sstore_sload_100}`
- `create_deploy/rex6/{create_10,create2_10}`
- `call_value_empty_account/rex6/{existing_account_50,empty_account_50}`
- `mixed_workload/rex6`

Still not REX6-enabled: `block_executor_*`, `eip7702_authlist`, `selfdestruct`, `salt_dynamic_gas`, and `oracle_real_data`.

## Approval gate and uncertainties

No lane should be created from this document alone. After approval, implementation must:

1. run each proposed entry twice on the same baseline and record its real stable fingerprint;
2. verify Criterion display names rather than relying only on registration-code inference;
3. capture runtime call-path evidence from both timed and differential sides;
4. reduce `editable` to the confirmed intersection;
5. demonstrate mutation sensitivity on one semantic and one performance perturbation.

This audit is static. It does not claim dynamic hotness; the fresh REX6 pipeline profile remains authoritative for frontier ordering.
