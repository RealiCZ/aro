//! ARO sweep/bench probe v3 for mega-evm: the v2 mixed workload (weighted toward
//! the machinery mega-evm ADDS on top of revm) pinned to the REX5 hardfork, so the
//! frontier covers the NEWEST instruction table / limits / precompiles. Runs on the
//! plain `transact_raw` path: `system_address` keeps its constructor default (the
//! SequencerRegistry resolution belongs to the BLOCK executor, out of scope here).
//!
//! Per-transaction program (raw bytecode, one contract + one callee):
//!   prologue  TIMESTAMP/COINBASE/NUMBER — activates gas detention, so every
//!             later opcode pays the mega-specific detention check
//!   per unit  16x ADD (compute-gas wrappers under detention)
//!             SSTORE+SLOAD over 8 warm slots (host inspect hooks)
//!             LOG2 with 32-byte data (dual compute+storage gas model)
//!             CALL into a small callee (call stack, sandbox/limit, account access)
//!
//! One reused caller, no state commit between txs: every tx re-runs identical
//! work on warm accounts, keeping the DB-stub share low by construction.
//!
//! Modes (same contract as the other ARO probes):
//!   sweep_hotloop_v3              bench: 5 samples of ns/tx -> `BENCH s1 .. s5`
//!                                 (reps scale with ARO_BENCH_SCALE)
//!   sweep_hotloop_v3 <spin_secs>  profile: spin until the deadline -> `SPUN n txs`

use std::hint::black_box;
use std::time::{Duration, Instant};

use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{MegaContext, MegaEvm, MegaSpecId, MegaTransaction};
use revm::{
    bytecode::opcode::{
        ADD, CALL, COINBASE, JUMPDEST, LOG2, MSTORE, NUMBER, POP, PUSH1, PUSH2, PUSH20, SLOAD,
        SSTORE, STOP, TIMESTAMP,
    },
    context::{ContextTr, TxEnv},
    database::{CacheDB, EmptyDB},
    primitives::TxKind,
    state::{AccountInfo, Bytecode},
};

// REX5: the newest hardfork. Per-tx execution needs NO registry seeding — the
// sequencer resolution from SequencerRegistry happens in the block executor's
// apply_pre_execution_changes, not on the transact_raw path this probe drives.
const SPEC: MegaSpecId = MegaSpecId::REX5;

const CONTRACT: Address = address!("0000000000000000000000000000000000100001");
const CALLEE: Address = address!("0000000000000000000000000000000000100002");
const CALLER: Address = address!("00000000000000000000000000000000001000aa");
const N_UNITS: u64 = 96;

fn set_account_code(db: &mut CacheDB<EmptyDB>, addr: Address, code: Bytes) {
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    let info = AccountInfo { code: Some(bytecode), code_hash, ..Default::default() };
    db.insert_account_info(addr, info);
}

/// Callee: touch volatile data + one warm SLOAD, then STOP. Small on purpose —
/// the point is the CALL edge (frames, account access, limits), not callee work.
fn build_callee() -> Vec<u8> {
    vec![TIMESTAMP, POP, PUSH1, 0, SLOAD, POP, STOP]
}

fn build_program(n_units: u64) -> Vec<u8> {
    let mut code: Vec<u8> = Vec::new();
    // Prologue: volatile-data access flips on gas detention for the rest of the tx.
    code.extend_from_slice(&[TIMESTAMP, POP, COINBASE, POP, NUMBER, POP]);
    // Seed 32 bytes of log payload at memory offset 0.
    code.extend_from_slice(&[PUSH1, 0xAA, PUSH1, 0x00, MSTORE]);
    for i in 0..n_units {
        // Compute: 16 ADDs, each paying the detention check + compute-gas wrapper.
        for j in 0..16u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
        }
        // Storage: SSTORE+SLOAD round-robin over 8 pre-warmed slots.
        let slot = (i % 8) as u8;
        code.extend_from_slice(&[PUSH1, slot + 1, PUSH1, slot, SSTORE]);
        code.extend_from_slice(&[PUSH1, slot, SLOAD, POP]);
        // LOG2, 32-byte payload: dual compute+storage gas.
        code.extend_from_slice(&[PUSH1, 0x01, PUSH1, 0x02, PUSH1, 32, PUSH1, 0, LOG2]);
        // CALL the callee: retLen retOff argsLen argsOff value addr gas, then CALL.
        code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH20]);
        code.extend_from_slice(CALLEE.as_slice());
        code.extend_from_slice(&[PUSH2, 0xFF, 0xFF, CALL, POP]);
        code.push(JUMPDEST);
    }
    code.push(STOP);
    code
}

fn main() {
    let spin_secs: Option<u64> = std::env::args().nth(1).and_then(|s| s.parse().ok());
    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, CONTRACT, build_program(N_UNITS).into());
    set_account_code(&mut db, CALLEE, build_callee().into());
    for slot in 0u8..8 {
        db.insert_account_storage(CONTRACT, U256::from(slot), U256::from(slot + 100))
            .expect("seed storage");
    }
    db.insert_account_info(
        CALLER,
        AccountInfo { balance: U256::from(10).pow(U256::from(18)), ..Default::default() },
    );

    let mut context = MegaContext::new(db, SPEC);
    context.chain_mut().operator_fee_scalar = Some(U256::from(0));
    context.chain_mut().operator_fee_constant = Some(U256::from(0));
    let mut evm = MegaEvm::new(context);

    let make_tx = || MegaTransaction {
        base: TxEnv {
            caller: CALLER,
            kind: TxKind::Call(CONTRACT),
            data: Bytes::default(),
            value: U256::ZERO,
            gas_limit: 30_000_000,
            nonce: 0,
            ..Default::default()
        },
        ..Default::default()
    };

    let mut run_tx = |acc: &mut u64| {
        let tx = make_tx();
        let r = alloy_evm::Evm::transact_raw(&mut evm, black_box(tx)).expect("tx ok");
        assert!(r.result.is_success(), "workload tx must succeed: {:?}", r.result);
        *acc = acc.wrapping_add(black_box(r.result.gas_used()));
    };

    let mut acc: u64 = 0;
    if let Some(secs) = spin_secs {
        // Profile mode: steady-state spin for the sampler.
        let deadline = Instant::now() + Duration::from_secs(secs);
        let mut n: u64 = 0;
        while Instant::now() < deadline {
            for _ in 0..16 {
                run_tx(&mut acc);
                n += 1;
            }
        }
        black_box(acc);
        println!("SPUN {} txs in {}s", n, secs);
    } else {
        // Bench mode: 5 samples of ns/tx; higher scales average more txs per sample.
        let reps = 200 * scale;
        // REX5 needs a LONG warmup: first-tx cost starts ~2.4x steady state and takes
        // several hundred txs to settle (lazy table/host initialization). Warm until
        // the deadline or the tx cap, whichever first, so the 5 timed samples are all
        // steady-state and the A/A floor stays honest.
        let warm_deadline = Instant::now() + Duration::from_millis(500);
        let mut warmed = 0u32;
        while warmed < 2048 && Instant::now() < warm_deadline {
            run_tx(&mut acc);
            warmed += 1;
        }
        let mut samples: Vec<f64> = Vec::new();
        for _ in 0..5 {
            let t = Instant::now();
            for _ in 0..reps {
                run_tx(&mut acc);
            }
            samples.push(t.elapsed().as_nanos() as f64 / reps as f64);
        }
        black_box(acc);
        let line =
            samples.iter().map(|s| format!("{:.0}", s)).collect::<Vec<_>>().join(" ");
        println!("BENCH {}", line);
    }
}
