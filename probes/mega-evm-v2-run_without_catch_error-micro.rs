//! ARO isolation micro-bench for `MegaHandler::run_without_catch_error`.
//!
//! `run_without_catch_error` is the body of a MegaETH transaction: it runs
//! `before_run` -> `validate` -> `pre_execution` -> `execution` ->
//! `post_execution` -> `execution_result`. The parent sweep probe
//! (`probes/sweep_hotloop_v2.rs`) reaches it through
//! `alloy_evm::Evm::transact_raw` -> `ExecuteEvm::transact_one` ->
//! `revm::handler::Handler::run`, where `run` is literally
//! `match self.run_without_catch_error(evm) { Ok(o) => Ok(o), Err(e) =>
//! self.catch_error(evm, e) }`. Because the workload transactions always
//! succeed, `run == run_without_catch_error` on this path, so calling the
//! target directly is measurement-equivalent to the parent while pinning the
//! timed region onto exactly one function.
//!
//! Everything outside the timed region (DB seeding, calldata pool
//! construction, handler allocation) is hoisted out; the timed loop does only
//! `set_tx` (a cheap move) + `run_without_catch_error`, so ~all self-time
//! lands inside the target.
//!
//! Input distribution: identical to the parent's per-tx program — one warm
//! reused caller repeatedly `CALL`ing a fixed 96-unit contract
//! (volatile-data prologue + per unit: 16x ADD under gas detention,
//! SSTORE/SLOAD over 8 warm slots, LOG2 w/ 32-byte payload, CALL into a small
//! callee). The only deterministic variation is transaction calldata length,
//! drawn from a fixed-seed xorshift in the realistic 0..=128 byte range that
//! ordinary contract calls carry; this mirrors real transaction diversity and
//! exercises MegaETH's calldata storage-gas accounting inside `validate`
//! (10x standard token rate) without perturbing success or the execution body.
//!
//! `ARO_BENCH_SCALE` (int, default 1) multiplies the inner repeat count:
//! same inputs, same path, more repeats per sample.
//!
//! Emits one final line: `BENCH s1 s2 s3 s4 s5` (ns per call).

use std::hint::black_box;
use std::time::Instant;

use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{
    MegaContext, MegaEvm, MegaHaltReason, MegaHandler, MegaSpecId, MegaTransaction,
    MegaTransactionError,
};
use revm::{
    bytecode::opcode::{
        ADD, CALL, COINBASE, JUMPDEST, LOG2, MSTORE, NUMBER, POP, PUSH1, PUSH2, PUSH20, SLOAD,
        SSTORE, STOP, TIMESTAMP,
    },
    context::{
        result::{EVMError, ExecutionResult},
        ContextSetters, ContextTr, TxEnv,
    },
    database::{CacheDB, EmptyDB},
    handler::{EthFrame, EvmTr, Handler},
    interpreter::interpreter::EthInterpreter,
    primitives::TxKind,
    state::{AccountInfo, Bytecode},
    ExecuteEvm,
};

// Matches the parent probe: the detention / dual-gas / limit machinery that
// `run_without_catch_error` drives is live under REX4 (REX5 needs
// SequencerRegistry system state seeded at pre-execution).
const SPEC: MegaSpecId = MegaSpecId::REX4;

const CONTRACT: Address = address!("0000000000000000000000000000000000100001");
const CALLEE: Address = address!("0000000000000000000000000000000000100002");
const CALLER: Address = address!("00000000000000000000000000000000001000aa");
const N_UNITS: u64 = 96;

/// Concrete DB error is `Infallible` for `CacheDB<EmptyDB>`.
type DbError = core::convert::Infallible;
type MicroError = EVMError<DbError, MegaTransactionError>;

/// Fixed-seed xorshift64 — deterministic, no system randomness.
#[inline]
fn xorshift(state: &mut u64) -> u64 {
    let mut x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *state = x;
    x
}

fn set_account_code(db: &mut CacheDB<EmptyDB>, addr: Address, code: Bytes) {
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    let info = AccountInfo { code: Some(bytecode), code_hash, ..Default::default() };
    db.insert_account_info(addr, info);
}

/// Callee: touch volatile data + one warm SLOAD, then STOP.
fn build_callee() -> Vec<u8> {
    vec![TIMESTAMP, POP, PUSH1, 0, SLOAD, POP, STOP]
}

/// Identical to the parent probe's per-tx program.
fn build_program(n_units: u64) -> Vec<u8> {
    let mut code: Vec<u8> = Vec::new();
    code.extend_from_slice(&[TIMESTAMP, POP, COINBASE, POP, NUMBER, POP]);
    code.extend_from_slice(&[PUSH1, 0xAA, PUSH1, 0x00, MSTORE]);
    for i in 0..n_units {
        for j in 0..16u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
        }
        let slot = (i % 8) as u8;
        code.extend_from_slice(&[PUSH1, slot + 1, PUSH1, slot, SSTORE]);
        code.extend_from_slice(&[PUSH1, slot, SLOAD, POP]);
        code.extend_from_slice(&[PUSH1, 0x01, PUSH1, 0x02, PUSH1, 32, PUSH1, 0, LOG2]);
        code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH20]);
        code.extend_from_slice(CALLEE.as_slice());
        code.extend_from_slice(&[PUSH2, 0xFF, 0xFF, CALL, POP]);
        code.push(JUMPDEST);
    }
    code.push(STOP);
    code
}

fn make_tx(data: Bytes) -> MegaTransaction {
    MegaTransaction {
        base: TxEnv {
            caller: CALLER,
            kind: TxKind::Call(CONTRACT),
            data,
            value: U256::ZERO,
            gas_limit: 30_000_000,
            nonce: 0,
            ..Default::default()
        },
        ..Default::default()
    }
}

fn main() {
    let scale: u64 =
        std::env::var("ARO_BENCH_SCALE").ok().and_then(|s| s.parse().ok()).unwrap_or(1);

    // ---- setup (outside the timed region) ----
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

    // Deterministic pool of realistic calldata payloads (0..=128 bytes),
    // built from a fixed-seed xorshift so inputs are reproducible.
    const POOL: usize = 64;
    let mut rng: u64 = 0x9E37_79B9_7F4A_7C15;
    let calldata_pool: Vec<Bytes> = (0..POOL)
        .map(|_| {
            let len = (xorshift(&mut rng) % 129) as usize;
            let bytes: Vec<u8> = (0..len).map(|_| (xorshift(&mut rng) & 0xff) as u8).collect();
            Bytes::from(bytes)
        })
        .collect();

    // Stateless handler; reused across calls exactly as revm's `run` would.
    let mut handler = MegaHandler::<_, _, EthFrame<EthInterpreter>>::new();

    // One direct call to the target. Returns gas used to feed the accumulator.
    let run_once = |evm: &mut MegaEvm<CacheDB<EmptyDB>, _, _>,
                        handler: &mut MegaHandler<_, MicroError, EthFrame<EthInterpreter>>,
                        data: Bytes|
     -> u64 {
        evm.ctx().set_tx(make_tx(data));
        let r: Result<ExecutionResult<MegaHaltReason>, MicroError> =
            Handler::run_without_catch_error(handler, evm);
        let r = r.expect("tx ok");
        assert!(r.is_success(), "workload tx must succeed: {r:?}");
        let gas = r.gas_used();
        // Drain the journal back out (as the parent's `transact` path does)
        // so each tx runs against the same cold DB base — a warm reused caller
        // whose nonce never accumulates. This overhead is tiny next to the
        // 96-unit execution body inside `run_without_catch_error`.
        black_box(ExecuteEvm::finalize(evm));
        gas
    };

    // ---- warmup ----
    let mut acc: u64 = 0;
    for i in 0..3usize {
        acc = acc.wrapping_add(run_once(
            &mut evm,
            &mut handler,
            calldata_pool[i % POOL].clone(),
        ));
    }

    // ---- timed samples ----
    let reps = 200u64 * scale;
    let mut samples: Vec<f64> = Vec::with_capacity(5);
    let mut idx: usize = 0;
    for _ in 0..5 {
        let t = Instant::now();
        for _ in 0..reps {
            let data = calldata_pool[idx % POOL].clone();
            idx += 1;
            acc = acc.wrapping_add(black_box(run_once(&mut evm, &mut handler, black_box(data))));
        }
        samples.push(t.elapsed().as_nanos() as f64 / reps as f64);
    }
    black_box(acc);

    let line = samples.iter().map(|s| format!("{s:.0}")).collect::<Vec<_>>().join(" ");
    println!("BENCH {line}");
}
