//! Differential fingerprint covering mega-evm frame-layer semantics.
//!
//! Extends the plain SLOAD/SSTORE/CREATE corpus of `evm_r3_diff` with scenarios that
//! exercise mechanisms gated on `MegaEvm::frame_init` / frame lifecycle:
//!   * system-contract interception (LimitControl / AccessControl)
//!   * oracle gas detention (CALL-based pre-Rex3, SLOAD-based Rex3+)
//!   * REX5 call-stack depth guard
//!   * REX4+ STORAGE_CALL_STIPEND for value-transferring CALL
//!   * per-frame resource limit lifecycle (inner REVERT discard, data/compute pressure)
//!
//! Spec matrix: MINI_REX, REX, REX3, REX4, REX5. Folds success, gas_used, returndata,
//! and read-back storage into one FNV-1a fingerprint printed as `DIFF <hex>`.
//!
//! No new deps; only mega-evm / revm / alloy packages already on the crate graph.

use std::hint::black_box;

use alloy_primitives::{address, Address, Bytes, U256};
use alloy_sol_types::SolCall;
use mega_evm::{
    constants::{
        mini_rex::ORACLE_ACCESS_COMPUTE_GAS as MINI_REX_ORACLE_DETENTION,
        rex::TX_COMPUTE_GAS_LIMIT as REX_TX_COMPUTE_GAS_LIMIT,
        rex3::ORACLE_ACCESS_COMPUTE_GAS as REX3_ORACLE_DETENTION,
        rex4::STORAGE_CALL_STIPEND,
    },
    IMegaAccessControl, IMegaLimitControl, MegaContext, MegaEvm, MegaSpecId, MegaTransaction,
    ACCESS_CONTROL_ADDRESS, LIMIT_CONTROL_ADDRESS, ORACLE_CONTRACT_ADDRESS,
};
use revm::{
    bytecode::opcode::{
        ADD, ADDRESS, CALL, CALLVALUE, CODECOPY, CREATE, DUP1, GAS, JUMPDEST, LOG1, MUL, MSTORE,
        POP, PUSH0, PUSH1, PUSH2, PUSH20, RETURN, REVERT, SLOAD, SSTORE, STATICCALL, STOP,
    },
    context::{ContextTr, TxEnv},
    database::{CacheDB, EmptyDB},
    primitives::{TxKind, CALL_STACK_LIMIT},
    state::Bytecode,
    Database,
};

// Keep constants referenced so a future refactor that drops them from the fold still
// fails to compile if someone removes the import — and documents the probe's intent.
const _: u64 = STORAGE_CALL_STIPEND;
const _: u64 = MINI_REX_ORACLE_DETENTION;
const _: u64 = REX3_ORACLE_DETENTION;
const _: u64 = REX_TX_COMPUTE_GAS_LIMIT;
const _: u64 = CALL_STACK_LIMIT;

#[inline]
fn fnv1a(mut h: u64, bytes: &[u8]) -> u64 {
    const PRIME: u64 = 0x0000_0100_0000_01B3;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    h
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn range(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn set_account_code(db: &mut CacheDB<EmptyDB>, addr: Address, code: Bytes) {
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    let mut info = db.basic(addr).ok().flatten().unwrap_or_default();
    info.code = Some(bytecode);
    info.code_hash = code_hash;
    db.insert_account_info(addr, info);
}

fn fund(db: &mut CacheDB<EmptyDB>, addr: Address, balance: U256) {
    let mut info = db.basic(addr).ok().flatten().unwrap_or_default();
    info.balance = balance;
    db.insert_account_info(addr, info);
}

fn push_u64(code: &mut Vec<u8>, v: u64) {
    if v <= 0xff {
        code.push(PUSH1);
        code.push(v as u8);
    } else if v <= 0xffff {
        code.push(PUSH2);
        code.extend_from_slice(&(v as u16).to_be_bytes());
    } else {
        // 8-byte push via PUSH1 length encoding is awkward; use successive ADD for large
        // constants is worse — emit PUSH1/PUSH2 only for our small constants.
        code.push(PUSH2);
        code.extend_from_slice(&((v.min(0xffff)) as u16).to_be_bytes());
    }
}

fn push_address(code: &mut Vec<u8>, addr: Address) {
    code.push(PUSH20);
    code.extend_from_slice(addr.as_slice());
}

/// CALL stack layout (bottom → top before opcode): retSize, retOffset, argsSize, argsOffset,
/// value, addr, gas — then CALL.
fn append_call(
    code: &mut Vec<u8>,
    target: Address,
    gas: u64,
    value: u64,
    args_off: u64,
    args_size: u64,
    ret_off: u64,
    ret_size: u64,
) {
    push_u64(code, ret_size);
    push_u64(code, ret_off);
    push_u64(code, args_size);
    push_u64(code, args_off);
    push_u64(code, value);
    push_address(code, target);
    push_u64(code, gas);
    code.push(CALL);
}

fn append_staticcall(
    code: &mut Vec<u8>,
    target: Address,
    gas: u64,
    args_off: u64,
    args_size: u64,
    ret_off: u64,
    ret_size: u64,
) {
    push_u64(code, ret_size);
    push_u64(code, ret_off);
    push_u64(code, args_size);
    push_u64(code, args_off);
    push_address(code, target);
    push_u64(code, gas);
    code.push(STATICCALL);
}

/// Store a 4-byte selector left-aligned in a 32-byte word at memory offset 0
/// (MSTORE writes 32 bytes; selector occupies the high 4 bytes of the word).
fn mstore_selector(code: &mut Vec<u8>, selector: [u8; 4]) {
    let mut word = [0u8; 32];
    word[0..4].copy_from_slice(&selector);
    // PUSH32 word; PUSH1 0; MSTORE
    code.push(0x7f); // PUSH32
    code.extend_from_slice(&word);
    code.push(PUSH1);
    code.push(0x00);
    code.push(MSTORE);
}

/// Outcome folded into the fingerprint for one execution.
struct Outcome {
    success: bool,
    gas: u64,
    output: Vec<u8>,
    slots: [U256; 6],
}

fn fold_outcome(fp: u64, o: &Outcome) -> u64 {
    let mut fp = fnv1a(fp, &[o.success as u8]);
    fp = fnv1a(fp, &o.gas.to_le_bytes());
    fp = fnv1a(fp, &(o.output.len() as u64).to_le_bytes());
    fp = fnv1a(fp, &o.output);
    for s in &o.slots {
        fp = fnv1a(fp, &s.to_le_bytes::<32>());
    }
    fp
}

fn run_tx(
    spec: MegaSpecId,
    db: CacheDB<EmptyDB>,
    caller: Address,
    to: Address,
    data: Bytes,
    value: U256,
    gas_limit: u64,
    read_slots_from: Option<Address>,
) -> Outcome {
    let mut context = MegaContext::new(db, spec);
    context.chain_mut().operator_fee_scalar = Some(U256::from(0));
    context.chain_mut().operator_fee_constant = Some(U256::from(0));
    let mut evm = MegaEvm::new(context);

    let tx = MegaTransaction {
        base: TxEnv {
            caller,
            kind: TxKind::Call(to),
            data,
            value,
            gas_limit,
            nonce: 0,
            ..Default::default()
        },
        ..Default::default()
    };

    let res = alloy_evm::Evm::transact_raw(&mut evm, tx);
    match res {
        Ok(r) => {
            let success = r.result.is_success();
            let gas = r.result.gas_used();
            let output = r.result.output().cloned().unwrap_or_default().to_vec();
            let mut slots = [U256::ZERO; 6];
            if let Some(addr) = read_slots_from {
                for slot in 0u8..6 {
                    slots[slot as usize] = r
                        .state
                        .get(&addr)
                        .and_then(|acc| acc.storage.get(&U256::from(slot)))
                        .map(|s| s.present_value)
                        .unwrap_or(U256::ZERO);
                }
            }
            Outcome { success, gas, output, slots }
        }
        Err(_) => Outcome {
            success: false,
            gas: u64::MAX,
            output: Vec::new(),
            slots: [U256::MAX; 6],
        },
    }
}

// ---------------------------------------------------------------------------
// Plain seeded corpus (template of evm_r3_diff, ~200 cases)
// ---------------------------------------------------------------------------

fn build_storage_program(rng: &mut Rng, do_create: bool) -> Vec<u8> {
    let mut code: Vec<u8> = Vec::new();

    if do_create {
        let _ = (CODECOPY,);
        code.push(PUSH1);
        code.push(0x00); // value
        code.push(PUSH1);
        code.push(0x00); // offset
        code.push(PUSH1);
        code.push(0x00); // size
        code.push(CREATE);
        code.push(POP);
    }

    let n_units = 6 + rng.range(10);
    for _ in 0..n_units {
        let slot = (rng.range(6)) as u8;
        let op = rng.range(3);
        match op {
            0 => {
                let value = (rng.range(4)) as u8;
                code.push(PUSH1);
                code.push(value);
                code.push(PUSH1);
                code.push(slot);
                code.push(SSTORE);
            }
            1 => {
                code.push(PUSH1);
                code.push(slot);
                code.push(SLOAD);
                code.push(POP);
            }
            _ => {
                code.push(PUSH1);
                code.push(slot);
                code.push(SLOAD);
                code.push(PUSH1);
                code.push(0x01);
                code.push(ADD);
                code.push(PUSH1);
                code.push(slot);
                code.push(SSTORE);
            }
        }
        code.push(JUMPDEST);
    }
    let _ = (MSTORE, PUSH2, DUP1, CALLVALUE);
    code.push(STOP);
    code
}

fn run_storage_case(spec: MegaSpecId, seed: u64) -> Outcome {
    let mut rng = Rng(seed | 1);
    let contract = address!("00000000000000000000000000000000000c0de1");
    let mut db = CacheDB::<EmptyDB>::default();
    let do_create = rng.range(2) == 0;
    set_account_code(&mut db, contract, build_storage_program(&mut rng, do_create).into());
    for slot in 0u8..6 {
        if rng.range(2) == 0 {
            let v = 100 + (rng.range(50)) as u8;
            db.insert_account_storage(contract, U256::from(slot), U256::from(v))
                .expect("seed storage");
        }
    }
    let caller = {
        let mut bytes = [0u8; 20];
        bytes[..8].copy_from_slice(&seed.to_be_bytes());
        bytes[19] = 0xaa;
        Address::from(bytes)
    };
    run_tx(
        spec,
        db,
        caller,
        contract,
        Bytes::default(),
        U256::ZERO,
        2_000_000_000,
        Some(contract),
    )
}

// ---------------------------------------------------------------------------
// A. System-contract interception
// ---------------------------------------------------------------------------

fn run_intercept_cases(spec: MegaSpecId, fp: u64) -> u64 {
    let remaining_sel = IMegaLimitControl::remainingComputeGasCall::SELECTOR;
    let is_disabled_sel = IMegaAccessControl::isVolatileDataAccessDisabledCall::SELECTOR;
    let unknown_sel = [0xde, 0xad, 0xbe, 0xef];

    let caller = address!("0000000000000000000000000000000000a110c1");
    let contract = address!("0000000000000000000000000000000000a110c2");

    // Variants: (target, selector, scheme_call=true/static=false, value, label-tag)
    let variants: &[(Address, [u8; 4], bool, u64)] = &[
        (LIMIT_CONTROL_ADDRESS, remaining_sel, true, 0),
        (LIMIT_CONTROL_ADDRESS, remaining_sel, false, 0),
        (LIMIT_CONTROL_ADDRESS, unknown_sel, true, 0),
        (LIMIT_CONTROL_ADDRESS, remaining_sel, true, 1), // non-zero value
        (ACCESS_CONTROL_ADDRESS, is_disabled_sel, true, 0),
        (ACCESS_CONTROL_ADDRESS, is_disabled_sel, false, 0),
        (ACCESS_CONTROL_ADDRESS, unknown_sel, true, 0),
        (ACCESS_CONTROL_ADDRESS, is_disabled_sel, true, 1),
    ];

    let mut fp = fp;
    for (ti, &(target, selector, is_call, value)) in variants.iter().enumerate() {
        let mut code: Vec<u8> = Vec::new();
        mstore_selector(&mut code, selector);
        if is_call {
            append_call(&mut code, target, 100_000, value, 0, 4, 0x20, 32);
        } else {
            append_staticcall(&mut code, target, 100_000, 0, 4, 0x20, 32);
        }
        code.push(POP);
        // Return 32 bytes of returndata area (may be empty under bypass).
        code.push(PUSH1);
        code.push(32);
        code.push(PUSH1);
        code.push(0x20);
        code.push(RETURN);

        let mut db = CacheDB::<EmptyDB>::default();
        set_account_code(&mut db, contract, code.into());
        fund(&mut db, caller, U256::from(10u64.pow(18)));
        fund(&mut db, contract, U256::from(10u64.pow(18)));

        // Also direct TX to system contract (no intermediate code).
        let o = run_tx(
            spec,
            db,
            caller,
            contract,
            Bytes::default(),
            U256::ZERO,
            50_000_000,
            None,
        );
        fp = fold_outcome(fp, &o);

        // Direct call path.
        let mut db2 = CacheDB::<EmptyDB>::default();
        fund(&mut db2, caller, U256::from(10u64.pow(18)));
        let o2 = run_tx(
            spec,
            db2,
            caller,
            target,
            Bytes::copy_from_slice(&selector),
            U256::from(value),
            50_000_000,
            None,
        );
        fp = fold_outcome(fp, &o2);
        let _ = ti;
    }
    fp
}

// ---------------------------------------------------------------------------
// B. Oracle detention
// ---------------------------------------------------------------------------

/// Append a tight gas-burn loop (JUMPDEST/JUMPI) that runs `iters` iterations then STOPs.
/// Jump targets are absolute PCs relative to the start of `code` (so this is safe to append
/// after a prefix).
fn append_compute_burn_loop(code: &mut Vec<u8>, iters: u32) {
    use revm::bytecode::opcode::{ISZERO, JUMP, JUMPI, SUB, SWAP1};
    code.push(PUSH1);
    code.push(0x01); // acc
    code.push(0x63); // PUSH4 iters
    code.extend_from_slice(&iters.to_be_bytes());
    let loop_pc = code.len() as u16;
    code.push(JUMPDEST);
    code.push(DUP1);
    code.push(ISZERO);
    code.push(PUSH2);
    let end_placeholder = code.len();
    code.extend_from_slice(&[0x00, 0x00]);
    code.push(JUMPI);
    code.push(PUSH1);
    code.push(0x01);
    code.push(SWAP1);
    code.push(SUB);
    code.push(SWAP1);
    code.push(PUSH1);
    code.push(0x01);
    code.push(ADD);
    code.push(DUP1);
    code.push(MUL);
    code.push(SWAP1);
    code.push(PUSH2);
    code.extend_from_slice(&loop_pc.to_be_bytes());
    code.push(JUMP);
    let end_pc = code.len() as u16;
    code[end_placeholder] = (end_pc >> 8) as u8;
    code[end_placeholder + 1] = (end_pc & 0xff) as u8;
    code.push(JUMPDEST);
    code.push(POP);
    code.push(POP);
    code.push(STOP);
}

fn build_compute_burn_loop(iters: u32) -> Vec<u8> {
    let mut code = Vec::new();
    append_compute_burn_loop(&mut code, iters);
    code
}

/// Pre-Rex3: CALL oracle then burn compute. Detention caps compute → early halt.
fn run_oracle_call_detention(spec: MegaSpecId, fp: u64) -> u64 {
    let caller = address!("0000000000000000000000000000000000b00101");
    let contract = address!("0000000000000000000000000000000000b00102");

    // CALL oracle (empty account is fine for CALL-based detection), then a long compute loop.
    let mut code: Vec<u8> = Vec::new();
    append_call(&mut code, ORACLE_CONTRACT_ADDRESS, 50_000, 0, 0, 0, 0, 0);
    code.push(POP);
    // Plenty of compute past the 1M pre-Rex3 detention budget.
    append_compute_burn_loop(&mut code, 500_000);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, contract, code.into());
    fund(&mut db, caller, U256::from(10u64.pow(18)));

    // gas_limit far above detention so the cap (not the tx limit) binds under clean MegaEvm.
    let o = run_tx(
        spec,
        db,
        caller,
        contract,
        Bytes::default(),
        U256::ZERO,
        500_000_000,
        None,
    );
    fold_outcome(fp, &o)
}

/// Rex3+: SLOAD on oracle triggers detention, then burn compute.
fn run_oracle_sload_detention(spec: MegaSpecId, fp: u64) -> u64 {
    let caller = address!("0000000000000000000000000000000000b00201");
    let contract = address!("0000000000000000000000000000000000b00202");

    // Deploy SLOAD-then-STOP code at the oracle address; caller CALLs it then burns.
    let oracle_code: Vec<u8> = vec![PUSH0, SLOAD, POP, STOP];

    let mut code: Vec<u8> = Vec::new();
    append_call(&mut code, ORACLE_CONTRACT_ADDRESS, 100_000, 0, 0, 0, 0, 0);
    code.push(POP);
    append_compute_burn_loop(&mut code, 500_000);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, ORACLE_CONTRACT_ADDRESS, oracle_code.into());
    set_account_code(&mut db, contract, code.into());
    fund(&mut db, caller, U256::from(10u64.pow(18)));

    let o = run_tx(
        spec,
        db,
        caller,
        contract,
        Bytes::default(),
        U256::ZERO,
        500_000_000,
        None,
    );
    fold_outcome(fp, &o)
}

// ---------------------------------------------------------------------------
// C. Deep call stack (REX5 guard)
// ---------------------------------------------------------------------------

fn run_deep_call_stack(spec: MegaSpecId, fp: u64) -> u64 {
    let caller = address!("0000000000000000000000000000000000c00101");
    let contract = address!("0000000000000000000000000000000000c00102");

    // Self-recursive: CALL self with remaining gas; eventually hits CALL_STACK_LIMIT.
    // Layout:
    //   JUMPDEST
    //   PUSH0 retSize / retOffset / argsSize / argsOffset / value
    //   ADDRESS
    //   GAS
    //   CALL
    //   POP
    //   STOP
    let mut code: Vec<u8> = Vec::new();
    code.push(JUMPDEST);
    code.push(PUSH0); // retSize
    code.push(PUSH0); // retOffset
    code.push(PUSH0); // argsSize
    code.push(PUSH0); // argsOffset
    code.push(PUSH0); // value
    code.push(ADDRESS);
    code.push(GAS);
    code.push(CALL);
    code.push(POP);
    code.push(STOP);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, contract, code.into());
    fund(&mut db, caller, U256::from(10u64.pow(18)));

    // Enough gas that (63/64)^depth still reaches depth > 1024 (see brief / CALL_STACK_LIMIT).
    let o = run_tx(
        spec,
        db,
        caller,
        contract,
        Bytes::default(),
        U256::ZERO,
        2_000_000_000,
        None,
    );
    fold_outcome(fp, &o)
}

// ---------------------------------------------------------------------------
// D. Storage stipend (value CALL + LOG1)
// ---------------------------------------------------------------------------

fn run_storage_stipend(spec: MegaSpecId, fp: u64) -> u64 {
    let caller = address!("0000000000000000000000000000000000d00101");
    let sender = address!("0000000000000000000000000000000000d00102");
    let receiver = address!("0000000000000000000000000000000000d00103");

    // Receiver: LOG1 with 32 bytes of data (costs > CALL_STIPEND storage gas under mega).
    // LOG1 stack: offset, size, topic0
    let mut recv: Vec<u8> = Vec::new();
    // write 32 zero bytes already in memory; emit LOG1
    recv.push(PUSH1);
    recv.push(0x00); // topic
    recv.push(PUSH1);
    recv.push(32); // size
    recv.push(PUSH1);
    recv.push(0x00); // offset
    recv.push(LOG1);
    recv.push(STOP);

    // Sender: CALL gas=0, value=1 wei → callee only has CALL_STIPEND (+ STORAGE_CALL_STIPEND on REX4+).
    let mut send: Vec<u8> = Vec::new();
    append_call(&mut send, receiver, 0, 1, 0, 0, 0, 0);
    // Return success flag in memory
    send.push(PUSH1);
    send.push(0x00);
    send.push(MSTORE);
    send.push(PUSH1);
    send.push(32);
    send.push(PUSH1);
    send.push(0x00);
    send.push(RETURN);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, receiver, recv.into());
    set_account_code(&mut db, sender, send.into());
    fund(&mut db, caller, U256::from(10u64.pow(18)));
    fund(&mut db, sender, U256::from(10u64.pow(18)));

    let o = run_tx(
        spec,
        db,
        caller,
        sender,
        Bytes::default(),
        U256::ZERO,
        50_000_000,
        None,
    );
    fold_outcome(fp, &o)
}

// ---------------------------------------------------------------------------
// E. Limit-frame lifecycle
// ---------------------------------------------------------------------------

/// Inner CALL SSTOREs then REVERTs; outer continues SSTOREing. Reverted usage discarded.
fn run_revert_frame_lifecycle(spec: MegaSpecId, fp: u64) -> u64 {
    let caller = address!("0000000000000000000000000000000000e00101");
    let outer = address!("0000000000000000000000000000000000e00102");
    let inner = address!("0000000000000000000000000000000000e00103");

    // Inner: SSTORE slots 0,1,2 then REVERT.
    let mut inner_code: Vec<u8> = Vec::new();
    for slot in 0u8..3 {
        inner_code.push(PUSH1);
        inner_code.push(0x2a + slot);
        inner_code.push(PUSH1);
        inner_code.push(slot);
        inner_code.push(SSTORE);
    }
    inner_code.push(PUSH1);
    inner_code.push(0x00);
    inner_code.push(PUSH1);
    inner_code.push(0x00);
    inner_code.push(REVERT);

    // Outer: SSTORE slot 0 = 1; CALL inner; SSTORE slot 1 = 2; SSTORE slot 2 = 3; STOP.
    let mut outer_code: Vec<u8> = Vec::new();
    outer_code.push(PUSH1);
    outer_code.push(0x01);
    outer_code.push(PUSH1);
    outer_code.push(0x00);
    outer_code.push(SSTORE);
    append_call(&mut outer_code, inner, 5_000_000, 0, 0, 0, 0, 0);
    outer_code.push(POP);
    outer_code.push(PUSH1);
    outer_code.push(0x02);
    outer_code.push(PUSH1);
    outer_code.push(0x01);
    outer_code.push(SSTORE);
    outer_code.push(PUSH1);
    outer_code.push(0x03);
    outer_code.push(PUSH1);
    outer_code.push(0x02);
    outer_code.push(SSTORE);
    outer_code.push(STOP);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, inner, inner_code.into());
    set_account_code(&mut db, outer, outer_code.into());
    fund(&mut db, caller, U256::from(10u64.pow(18)));

    let o = run_tx(
        spec,
        db,
        caller,
        outer,
        Bytes::default(),
        U256::ZERO,
        100_000_000,
        Some(outer),
    );
    fold_outcome(fp, &o)
}

/// Data-size heavy: LOG with multi-KB data (hits data-size accounting).
fn run_data_heavy(spec: MegaSpecId, fp: u64) -> u64 {
    let caller = address!("0000000000000000000000000000000000e00201");
    let contract = address!("0000000000000000000000000000000000e00202");

    // Emit several LOG1 with 4KB data each.
    let data_size: u16 = 4096;
    let mut code: Vec<u8> = Vec::new();
    // Expand memory cheaply with MSTORE at high offset.
    code.push(PUSH1);
    code.push(0x01);
    code.push(PUSH2);
    code.extend_from_slice(&(data_size - 32).to_be_bytes());
    code.push(MSTORE);
    for topic in 0u8..8 {
        code.push(PUSH1);
        code.push(topic);
        code.push(PUSH2);
        code.extend_from_slice(&data_size.to_be_bytes());
        code.push(PUSH1);
        code.push(0x00);
        code.push(LOG1);
    }
    code.push(STOP);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, contract, code.into());
    fund(&mut db, caller, U256::from(10u64.pow(18)));

    let o = run_tx(
        spec,
        db,
        caller,
        contract,
        Bytes::default(),
        U256::ZERO,
        500_000_000,
        None,
    );
    fold_outcome(fp, &o)
}

/// Compute-heavy loop with gas_limit far above per-spec compute limit.
fn run_compute_heavy(spec: MegaSpecId, fp: u64) -> u64 {
    let caller = address!("0000000000000000000000000000000000e00301");
    let contract = address!("0000000000000000000000000000000000e00302");

    // Large iter count: under REX the 200M compute limit should bind before the loop finishes.
    let code = build_compute_burn_loop(50_000_000);

    let mut db = CacheDB::<EmptyDB>::default();
    set_account_code(&mut db, contract, code.into());
    fund(&mut db, caller, U256::from(10u64.pow(18)));

    let o = run_tx(
        spec,
        db,
        caller,
        contract,
        Bytes::default(),
        U256::ZERO,
        2_000_000_000, // far above REX TX_COMPUTE_GAS_LIMIT (200M)
        None,
    );
    fold_outcome(fp, &o)
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn main() {
    let specs = [
        MegaSpecId::MINI_REX,
        MegaSpecId::REX,
        MegaSpecId::REX3,
        MegaSpecId::REX4,
        MegaSpecId::REX5,
    ];

    const BASIS: u64 = 0xcbf2_9ce4_8422_2325; // FNV-1a offset basis
    let mut fp: u64 = BASIS;
    // When set, also print independent per-group fingerprints on stderr (for fixture diagnosis).
    let group_fps = std::env::var_os("ARO_SEMANTICS_GROUPS").is_some();

    // --- compact plain corpus (~200 seeds × 5 specs) ---
    let cases: u64 = 200;
    let mut g_storage = BASIS;
    for i in 0..cases {
        let seed = 0x9E37_79B9_7F4A_7C15u64.wrapping_mul(i + 1) ^ (i << 33);
        for (si, &spec) in specs.iter().enumerate() {
            let o = run_storage_case(spec, seed ^ ((si as u64) << 48));
            fp = fold_outcome(fp, &o);
            if group_fps {
                g_storage = fold_outcome(g_storage, &o);
            }
        }
    }

    // --- mega-semantics scenarios (each outcome folded once into the global fingerprint) ---
    let mut g_intercept = BASIS;
    let mut g_oracle = BASIS;
    let mut g_deep = BASIS;
    let mut g_stipend = BASIS;
    let mut g_lifecycle = BASIS;

    for &spec in &specs {
        // A. Interception
        fp = run_intercept_cases(spec, fp);
        if group_fps {
            g_intercept = run_intercept_cases(spec, g_intercept);
        }

        // B. Oracle detention (CALL-based + SLOAD-based)
        fp = run_oracle_call_detention(spec, fp);
        fp = run_oracle_sload_detention(spec, fp);
        if group_fps {
            g_oracle = run_oracle_call_detention(spec, g_oracle);
            g_oracle = run_oracle_sload_detention(spec, g_oracle);
        }

        // C. Deep call stack (REX5 guard; still run all specs for breadth)
        fp = run_deep_call_stack(spec, fp);
        if group_fps {
            g_deep = run_deep_call_stack(spec, g_deep);
        }

        // D. Storage stipend
        fp = run_storage_stipend(spec, fp);
        if group_fps {
            g_stipend = run_storage_stipend(spec, g_stipend);
        }

        // E. Limit-frame lifecycle
        fp = run_revert_frame_lifecycle(spec, fp);
        fp = run_data_heavy(spec, fp);
        fp = run_compute_heavy(spec, fp);
        if group_fps {
            g_lifecycle = run_revert_frame_lifecycle(spec, g_lifecycle);
            g_lifecycle = run_data_heavy(spec, g_lifecycle);
            g_lifecycle = run_compute_heavy(spec, g_lifecycle);
        }
    }

    if group_fps {
        eprintln!("DIFF_storage   {:016x}", g_storage);
        eprintln!("DIFF_intercept {:016x}", g_intercept);
        eprintln!("DIFF_oracle    {:016x}", g_oracle);
        eprintln!("DIFF_deep      {:016x}", g_deep);
        eprintln!("DIFF_stipend   {:016x}", g_stipend);
        eprintln!("DIFF_lifecycle {:016x}", g_lifecycle);
    }

    black_box(fp);
    println!("DIFF {:016x}", fp);
}
