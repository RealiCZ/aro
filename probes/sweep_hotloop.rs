//! Spin-capable hot-loop for `aro sweep` profiling of mega-evm.
//!
//! Drives a representative mixed SLOAD/SSTORE EVM workload through the real public
//! `transact_raw` API (REX4) in a CONTINUOUS loop for `<spin_secs>` seconds (first CLI
//! arg, default 8), so macOS `sample` can attribute steady-state self-time across the
//! WHOLE crate's hot path — the interpreter dispatch, the per-opcode compute-gas
//! wrappers, the limit trackers, and the host inspect hooks. This is the broad profile
//! the frontier map is built from (NOT a narrow microbench).
//!
//!   sweep_hotloop <spin_secs>

use std::hint::black_box;
use std::time::{Duration, Instant};

use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{MegaContext, MegaEvm, MegaSpecId, MegaTransaction};
use revm::{
    bytecode::opcode::{JUMPDEST, POP, PUSH1, SLOAD, SSTORE, STOP},
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

/// `n_units` storage-access units against a small slot set: each unit SSTORE+SLOAD on a
/// slot in {0,1,2,3} round-robin, so after the first 4 units every access is slot-present.
fn build_program(n_units: u64) -> Vec<u8> {
    let mut code: Vec<u8> = Vec::with_capacity((n_units as usize) * 11 + 4);
    for i in 0..n_units {
        let slot = (i % 4) as u8;
        let value = slot + 1;
        code.extend_from_slice(&[PUSH1, value, PUSH1, slot, SSTORE]);
        code.extend_from_slice(&[PUSH1, slot, SLOAD, POP]);
        code.push(JUMPDEST);
    }
    code.push(STOP);
    code
}

fn main() {
    let spin_secs: u64 = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(8);
    let n_units: u64 = 1_500;
    let contract = address!("0000000000000000000000000000000000100001");
    let spec = MegaSpecId::REX4;

    let caller_for = |i: u64| -> Address {
        let mut bytes = [0u8; 20];
        bytes[..8].copy_from_slice(&i.to_be_bytes());
        bytes[19] = 0xaa;
        Address::from(bytes)
    };

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, contract, build_program(n_units).into());
    for slot in 0u8..4 {
        db.insert_account_storage(contract, U256::from(slot), U256::from(slot + 100))
            .expect("seed storage");
    }

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

    // Spin: keep executing transactions until the deadline so `sample` profiles steady state.
    let deadline = Instant::now() + Duration::from_secs(spin_secs);
    let mut i: u64 = 0;
    let mut acc: u64 = 0;
    while Instant::now() < deadline {
        for _ in 0..64 {
            let tx = make_tx(i);
            let r = alloy_evm::Evm::transact_raw(&mut evm, black_box(tx)).expect("tx ok");
            acc = acc.wrapping_add(black_box(r.result.gas_used()));
            i = i.wrapping_add(1);
        }
    }
    black_box(acc);
    println!("SPUN {} txs in {}s", i, spin_secs);
}
