//! Microbench for the mega-evm REX4 `host::inspect_storage` slot-present hot path.
//!
//! Drives a contract that performs many SLOAD / SSTORE operations on a small set of storage
//! slots through the real public `transact_raw` API on the REX4 spec. SLOAD and SSTORE both
//! route through `JournalInspectTr::inspect_storage`, whose REX4 slot-present branch is the
//! site the optimization targets (it used to re-run a tail `inspect_account` purely for the
//! borrow checker). After a slot's first touch it is present in the journal, so every
//! subsequent SLOAD/SSTORE on it hits the slot-present branch — exactly the eliminated path.
//!
//! Prints ONE line `BENCH <ns> ...` = mean ns per executed transaction.
//! Honors ARO_BENCH_SCALE: multiplies the per-tx SLOAD/SSTORE loop count (batch) by `scale`,
//! so each timed sample averages more hot-path work and the A/A floor drops without changing
//! the path or the inputs.

use std::hint::black_box;
use std::time::Instant;

use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{MegaContext, MegaEvm, MegaSpecId, MegaTransaction};
use revm::{
    bytecode::opcode::{
        ADD, DUP1, DUP2, JUMPDEST, MSTORE, POP, PUSH1, PUSH2, SLOAD, SSTORE, STOP,
    },
    context::{ContextTr, TxEnv},
    database::{CacheDB, EmptyDB},
    primitives::TxKind,
    state::{AccountInfo, Bytecode},
};

fn set_account_code(db: &mut CacheDB<EmptyDB>, addr: Address, code: Bytes) {
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    let info = AccountInfo { code: Some(bytecode), code_hash, ..Default::default() };
    db.insert_account_info(addr, info);
}

/// Build a straight-line program performing `n_units` storage-access units against a fixed
/// small slot set. Each unit:
///   SSTORE slot, value   (writes a present slot after first touch)
///   SLOAD  slot ; POP    (reads the same present slot)
/// over slots {0,1,2,3} round-robin, so after the first 4 units every access is slot-present.
fn build_program(n_units: u64) -> Vec<u8> {
    let mut code: Vec<u8> = Vec::with_capacity((n_units as usize) * 12 + 4);
    for i in 0..n_units {
        let slot = (i % 4) as u8; // small working set -> warm/present slots
        // value = slot + 1 (non-zero, so the slot stays non-zero/present)
        let value = slot + 1;
        // SSTORE: push value, push slot, SSTORE
        code.push(PUSH1);
        code.push(value);
        code.push(PUSH1);
        code.push(slot);
        code.push(SSTORE);
        // SLOAD: push slot, SLOAD, POP
        code.push(PUSH1);
        code.push(slot);
        code.push(SLOAD);
        code.push(POP);
        // padding op to vary the stream
        code.push(JUMPDEST);
    }
    code.push(STOP);
    code
}

fn main() {
    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);

    let n_units: u64 = 1_500 * scale;

    let contract = address!("0000000000000000000000000000000000100001");
    let spec = MegaSpecId::REX4;

    // Derive a fresh caller per tx so every tx runs at nonce 0 (transact_raw does not commit
    // nonce changes back to the DB). The contract storage starts empty each tx, so the first
    // touch of each of the 4 slots is absent and the rest are slot-present — the hot path.
    let caller_for = |i: u64| -> Address {
        let mut bytes = [0u8; 20];
        bytes[..8].copy_from_slice(&i.to_be_bytes());
        bytes[19] = 0xaa;
        Address::from(bytes)
    };

    // Pre-fund the slots in the DB so SLOAD/SSTORE see committed non-zero originals and the
    // slot-present branch is exercised from the very first access too. (CacheDB storage.)
    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, contract, build_program(n_units).into());
    // Seed slots 0..4 with non-zero original values in the contract account storage.
    for slot in 0u8..4 {
        db.insert_account_storage(contract, U256::from(slot), U256::from(slot + 100))
            .expect("seed storage");
    }
    // Touch MSTORE/PUSH2/DUP imports so the optimizer keeps the use-set stable.
    let _ = (MSTORE, PUSH2, DUP1, DUP2, ADD);

    let mut context = MegaContext::new(db, spec);
    context.chain_mut().operator_fee_scalar = Some(U256::from(0));
    context.chain_mut().operator_fee_constant = Some(U256::from(0));
    let mut evm = MegaEvm::new(context);

    let make_tx = |i: u64| MegaTransaction {
        base: TxEnv {
            caller: caller_for(i),
            kind: TxKind::Call(contract),
            data: Bytes::default(),
            value: U256::ZERO,
            gas_limit: 2_000_000_000,
            nonce: 0,
            ..Default::default()
        },
        ..Default::default()
    };

    // Warmup.
    let warmup: u64 = 20;
    for i in 0..warmup {
        let tx = make_tx(i);
        let r = alloy_evm::Evm::transact_raw(&mut evm, black_box(tx)).expect("tx ok");
        black_box(&r);
    }

    let iters: u64 = 200;
    let mut acc: u64 = 0;
    let start = Instant::now();
    for i in 0..iters {
        let tx = make_tx(warmup + i);
        let r = alloy_evm::Evm::transact_raw(&mut evm, black_box(tx)).expect("tx ok");
        acc = acc.wrapping_add(black_box(r.result.gas_used()));
        black_box(&r);
    }
    let elapsed = start.elapsed();
    black_box(acc);

    let ns_per_tx = (elapsed.as_nanos() as f64) / (iters as f64);
    println!(
        "BENCH {:.1} ns_per_tx iters={} n_units={} scale={} acc={}",
        ns_per_tx, iters, n_units, scale, acc
    );
}
