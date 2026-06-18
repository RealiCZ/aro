//! Microbench probe for mega-evm's per-opcode limit-check hot path (round 2, evm-b).
//!
//! Every EVM opcode in the MegaEvm instruction table funnels through
//! `compute_gas!` -> `AdditionalLimit::record_compute_gas` ->
//! `AdditionalLimit::check_limit`, which fans out to the four per-dimension
//! trackers (data_size, kv_update, compute_gas, state_growth). Each tracker's
//! `check_limit` (REX4+) calls `FrameLimitTracker::exceeds_current_frame_limit`,
//! the single most-executed leaf helper on the steady-state loop. Profiling a
//! compute-heavy transaction (a long PUSH/PUSH/ADD/POP loop) shows
//! `AdditionalLimit::check_limit` (~22-23% of in-binary compute) as the heaviest
//! controllable in-binary leaf, with `exceeds_current_frame_limit` inlined into
//! the per-tracker `check_limit` bodies.
//!
//! Round 2 (evm-b) eliminates the dead `checked_add(...).expect("overflow")`
//! overflow check (and its panic landing pad) from the per-frame fast path of
//! `exceeds_current_frame_limit`: a single frame's `persistent + discardable`
//! usage cannot overflow u64 within one transaction, so the branch is dead. The
//! value produced is identical; only the dead overflow check is removed.
//!
//! This probe drives the path through the real public `execute_transaction` API:
//! it executes a fixed compute-heavy contract once per timed sample and prints
//! the per-call nanoseconds. The kernel under test is exercised thousands of
//! times per tx, so the per-tx time is dominated by it.
//!
//! `argv[1]`, if present, is a spin-duration in seconds: the probe loops
//! `run_tx` for that long (no timing output) so the CPU profiler can sample a
//! steady hot loop. With no arg it runs the timed BENCH path.
//!
//! Prints one line: `BENCH <ns> <ns> ...` (per-call nanosecond samples).
#![allow(missing_docs)]

use std::hint::black_box;
use std::time::{Duration, Instant};

use mega_evm::{
    alloy_primitives::{address, Address, Bytes, U256},
    revm::bytecode::opcode::{ADD, POP},
    revm::inspector::NoOpInspector,
    test_utils::{BytecodeBuilder, MemoryDatabase},
    EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId, MegaTransaction,
};
use revm::context::tx::TxEnvBuilder;

const CALLER: Address = address!("0000000000000000000000000000000000100000");
const CONTRACT: Address = address!("0000000000000000000000000000000000100002");
const FEATURE_GAS_LIMIT: u64 = 10_000_000_000;

/// `iterations` copies of `PUSH1 1 PUSH1 2 ADD POP` — a pure compute loop with
/// no storage/call side effects, so `check_limit` is hit on every opcode.
fn compute_heavy_code(iterations: usize) -> Bytes {
    let mut builder = BytecodeBuilder::default();
    for _ in 0..iterations {
        builder = builder.push_number(1u64).push_number(2u64).append(ADD).append(POP);
    }
    builder.build()
}

fn build_db(code: &Bytes) -> MemoryDatabase {
    MemoryDatabase::default()
        .account_code(CONTRACT, code.clone())
        .account_balance(CALLER, U256::from(10).pow(U256::from(18)))
}

fn tx_env() -> revm::context::TxEnv {
    TxEnvBuilder::new()
        .caller(CALLER)
        .call(CONTRACT)
        .gas_limit(FEATURE_GAS_LIMIT)
        .value(U256::ZERO)
        .data(Bytes::new())
        .build_fill()
}

/// Run one compute-heavy transaction on a fresh MegaEvm at the given spec.
fn run_tx(spec: MegaSpecId, code: &Bytes) {
    let mut context = MegaContext::new(build_db(code), spec);
    context.modify_chain(|chain| {
        chain.operator_fee_scalar = Some(U256::ZERO);
        chain.operator_fee_constant = Some(U256::ZERO);
    });
    let mut evm = MegaEvm::<_, NoOpInspector, EmptyExternalEnv>::new(context);
    let mut mega_tx = MegaTransaction::new(tx_env());
    mega_tx.enveloped_tx = Some(Bytes::new());
    let r = evm.execute_transaction(black_box(mega_tx)).expect("mega transact");
    assert!(r.result.is_success());
    black_box(r);
}

fn main() {
    // REX4 exercises the per-frame state-growth path; it is the heaviest mega spec.
    let spec = MegaSpecId::REX4;
    let code = compute_heavy_code(2000);

    // Profiler mode: `argv[1]` = spin seconds. Loop the kernel so `sample` can
    // capture a stable steady-state hot loop. No timing output in this mode.
    if let Some(arg) = std::env::args().nth(1) {
        if let Ok(secs) = arg.parse::<u64>() {
            let deadline = Instant::now() + Duration::from_secs(secs);
            while Instant::now() < deadline {
                for _ in 0..16 {
                    run_tx(black_box(spec), black_box(&code));
                }
            }
            return;
        }
    }

    // Warm up.
    for _ in 0..50 {
        run_tx(spec, &code);
    }

    const SAMPLES: usize = 200;
    let mut ns: Vec<u128> = Vec::with_capacity(SAMPLES);
    for _ in 0..SAMPLES {
        let t0 = Instant::now();
        run_tx(black_box(spec), black_box(&code));
        ns.push(t0.elapsed().as_nanos());
    }

    let mut line = String::from("BENCH");
    for v in &ns {
        line.push(' ');
        line.push_str(&v.to_string());
    }
    println!("{line}");
}
