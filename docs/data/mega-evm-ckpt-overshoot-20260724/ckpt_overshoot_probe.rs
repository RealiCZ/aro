//! Disposable measurement probe: per-opcode gas stream via revm Inspector.
//! Isolation-only — do not merge into mega-evm production.

use std::cell::RefCell;
use std::env;
use std::io::{self, Write};

use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId, MegaTransaction};
use revm::{
    bytecode::opcode::{
        self, ADD, CALL, COINBASE, CREATE, JUMPDEST, JUMPI, LOG1, LOG2, LT, MUL, MSTORE, NUMBER,
        POP, PUSH1, PUSH2, PUSH20, RETURN, REVERT, SLOAD, SSTORE, STOP, SUB, TIMESTAMP,
    },
    context::{ContextTr, JournalTr, TxEnv},
    database::{CacheDB, EmptyDB},
    inspector::Inspector,
    interpreter::{
        interpreter_types::Jumps, CallInputs, CallOutcome, CallScheme, CreateInputs, CreateOutcome,
        CreateScheme, Interpreter,
    },
    primitives::TxKind,
    state::{AccountInfo, Bytecode},
    Database, InspectEvm,
};

type Ctx = MegaContext<CacheDB<EmptyDB>, EmptyExternalEnv>;

thread_local! {
    static STEPS: RefCell<Vec<(u8, u64, u8)>> = const { RefCell::new(Vec::new()) };
    static LAST: RefCell<Option<(u8, u64, u8)>> = const { RefCell::new(None) };
}

#[derive(Default)]
struct TlsStreamInspector;

impl Inspector<Ctx> for TlsStreamInspector {
    fn step(&mut self, interp: &mut Interpreter, context: &mut Ctx) {
        let op = interp.bytecode.opcode();
        let depth = context.journal_ref().depth().min(255) as u8;
        LAST.with(|l| *l.borrow_mut() = Some((op, interp.gas.remaining(), depth)));
    }

    fn step_end(&mut self, interp: &mut Interpreter, _context: &mut Ctx) {
        LAST.with(|l| {
            if let Some((op, gas_before, depth)) = l.borrow_mut().take() {
                let cost = gas_before.saturating_sub(interp.gas.remaining());
                STEPS.with(|s| s.borrow_mut().push((op, cost, depth)));
            }
        });
    }

    fn call_end(&mut self, context: &mut Ctx, inputs: &CallInputs, _outcome: &mut CallOutcome) {
        if context.journal_ref().depth() == 0 {
            return;
        }
        let want = match inputs.scheme {
            CallScheme::Call => opcode::CALL,
            CallScheme::CallCode => opcode::CALLCODE,
            CallScheme::DelegateCall => opcode::DELEGATECALL,
            CallScheme::StaticCall => opcode::STATICCALL,
        };
        STEPS.with(|s| {
            if let Some((op, cost, _)) = s.borrow_mut().last_mut() {
                if *op == want {
                    *cost = cost.saturating_sub(inputs.gas_limit);
                }
            }
        });
    }

    fn create_end(
        &mut self,
        context: &mut Ctx,
        inputs: &CreateInputs,
        _outcome: &mut CreateOutcome,
    ) {
        if context.journal_ref().depth() == 0 {
            return;
        }
        let want = match inputs.scheme {
            CreateScheme::Create => opcode::CREATE,
            CreateScheme::Create2 { .. } => opcode::CREATE2,
            _ => opcode::CREATE,
        };
        STEPS.with(|s| {
            if let Some((op, cost, _)) = s.borrow_mut().last_mut() {
                if *op == want {
                    *cost = cost.saturating_sub(inputs.gas_limit);
                }
            }
        });
    }
}

fn clear_tls() {
    STEPS.with(|s| s.borrow_mut().clear());
    LAST.with(|l| *l.borrow_mut() = None);
}

fn take_tls() -> Vec<(u8, u64, u8)> {
    STEPS.with(|s| std::mem::take(&mut *s.borrow_mut()))
}

fn set_code(db: &mut CacheDB<EmptyDB>, addr: Address, code: Bytes) {
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    db.insert_account_info(
        addr,
        AccountInfo {
            code: Some(bytecode),
            code_hash,
            ..Default::default()
        },
    );
}

fn fund(db: &mut CacheDB<EmptyDB>, addr: Address, bal: u64) {
    let mut info = Database::basic(db, addr).ok().flatten().unwrap_or_default();
    info.balance = U256::from(bal);
    db.insert_account_info(addr, info);
}

fn push20(code: &mut Vec<u8>, addr: Address) {
    code.push(PUSH20);
    code.extend_from_slice(addr.as_slice());
}

fn push8(code: &mut Vec<u8>, v: u64) {
    code.push(opcode::PUSH8);
    code.extend_from_slice(&v.to_be_bytes());
}

fn emit_meta(wl: &str, spec: &str, note: &str) {
    println!(
        r#"{{"type":"meta","wl":"{}","spec":"{}","note":"{}"}}"#,
        wl,
        spec,
        note.replace('"', "'")
    );
}

fn exec_one(
    wl: &str,
    tx_id: u32,
    spec: MegaSpecId,
    mut db: CacheDB<EmptyDB>,
    caller: Address,
    to: Address,
    gas_limit: u64,
) {
    fund(&mut db, caller, 10u64.pow(18));
    clear_tls();
    let mut context = MegaContext::new(db, spec);
    context.chain_mut().operator_fee_scalar = Some(U256::from(0));
    context.chain_mut().operator_fee_constant = Some(U256::from(0));
    let mut evm = MegaEvm::new(context).with_inspector(TlsStreamInspector);
    let tx = MegaTransaction {
        base: TxEnv {
            caller,
            kind: TxKind::Call(to),
            data: Bytes::default(),
            value: U256::ZERO,
            gas_limit,
            nonce: 0,
            ..Default::default()
        },
        ..Default::default()
    };
    let (ok, gas_used) = match InspectEvm::inspect_tx(&mut evm, tx) {
        Ok(r) => (r.result.is_success(), r.result.gas_used()),
        Err(_) => (false, u64::MAX),
    };
    let steps = take_tls();
    let mut out = io::stdout().lock();
    for (i, (op, gas, depth)) in steps.iter().enumerate() {
        let _ = writeln!(
            out,
            r#"{{"type":"step","wl":"{}","tx":{},"i":{},"op":{},"gas":{},"depth":{}}}"#,
            wl, tx_id, i, op, gas, depth
        );
    }
    let _ = writeln!(
        out,
        r#"{{"type":"tx_end","wl":"{}","tx":{},"steps":{},"gas_used":{},"ok":{}}}"#,
        wl,
        tx_id,
        steps.len(),
        gas_used,
        ok
    );
}

const CALLER: Address = address!("00000000000000000000000000000000001000aa");

fn wl_sweep_hotloop_v2() {
    const SPEC: MegaSpecId = MegaSpecId::REX4;
    const CONTRACT: Address = address!("0000000000000000000000000000000000100001");
    const CALLEE: Address = address!("0000000000000000000000000000000000100002");
    const N_UNITS: u64 = 96;
    emit_meta(
        "sweep_hotloop_v2",
        "REX4",
        "ARO probe: detention+ADD*16+SSTORE/SLOAD+LOG2+CALL x96",
    );
    let mut db = CacheDB::<EmptyDB>::default();
    set_code(
        &mut db,
        CALLEE,
        vec![TIMESTAMP, POP, PUSH1, 0, SLOAD, POP, STOP].into(),
    );
    let mut code: Vec<u8> = Vec::new();
    code.extend_from_slice(&[TIMESTAMP, POP, COINBASE, POP, NUMBER, POP]);
    code.extend_from_slice(&[PUSH1, 0xAA, PUSH1, 0x00, MSTORE]);
    for i in 0..N_UNITS {
        for j in 0..16u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
        }
        let slot = (i % 8) as u8;
        code.extend_from_slice(&[PUSH1, slot + 1, PUSH1, slot, SSTORE]);
        code.extend_from_slice(&[PUSH1, slot, SLOAD, POP]);
        code.extend_from_slice(&[PUSH1, 0x01, PUSH1, 0x02, PUSH1, 32, PUSH1, 0, LOG2]);
        code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0]);
        push20(&mut code, CALLEE);
        code.extend_from_slice(&[PUSH2, 0x75, 0x30, CALL, POP]);
    }
    code.push(STOP);
    set_code(&mut db, CONTRACT, code.into());
    for slot in 0u8..8 {
        let _ = db.insert_account_storage(CONTRACT, U256::from(slot), U256::from(1));
    }
    exec_one("sweep_hotloop_v2", 0, SPEC, db, CALLER, CONTRACT, 2_000_000_000);
}

fn wl_rex6_sstore_log() {
    const SPEC: MegaSpecId = MegaSpecId::REX6;
    const CONTRACT: Address = address!("0000000000000000000000000000000000100002");
    emit_meta(
        "rex6_sstore_log",
        "REX6",
        "lane1-shaped: 100 SSTORE + 50 SLOAD + 50 LOG1",
    );
    let mut db = CacheDB::<EmptyDB>::default();
    let mut code = Vec::new();
    for i in 0..100u64 {
        push8(&mut code, i + 1);
        push8(&mut code, i);
        code.push(SSTORE);
    }
    for i in 0..50u64 {
        push8(&mut code, i);
        code.extend_from_slice(&[SLOAD, POP]);
    }
    for _ in 0..50u64 {
        push8(&mut code, 0x11);
        push8(&mut code, 0);
        push8(&mut code, 0);
        code.push(LOG1);
    }
    code.push(STOP);
    set_code(&mut db, CONTRACT, code.into());
    exec_one(
        "rex6_sstore_log",
        0,
        SPEC,
        db,
        CALLER,
        CONTRACT,
        10_000_000_000,
    );
}

fn wl_rex6_create_shaped() {
    const SPEC: MegaSpecId = MegaSpecId::REX6;
    const CONTRACT: Address = address!("0000000000000000000000000000000000100003");
    emit_meta("rex6_create_shaped", "REX6", "CREATE x20 + arithmetic padding");
    let mut db = CacheDB::<EmptyDB>::default();
    let mut code = Vec::new();
    code.extend_from_slice(&[PUSH1, STOP, PUSH1, 0x00, MSTORE]);
    for _ in 0..20u8 {
        code.extend_from_slice(&[PUSH1, 0x00, PUSH1, 0x01, PUSH1, 0x1f, CREATE, POP]);
        for j in 0..32u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
        }
    }
    code.push(STOP);
    set_code(&mut db, CONTRACT, code.into());
    exec_one(
        "rex6_create_shaped",
        0,
        SPEC,
        db,
        CALLER,
        CONTRACT,
        10_000_000_000,
    );
}

fn wl_rex6_selfdestruct_shaped() {
    const SPEC: MegaSpecId = MegaSpecId::REX6;
    const CONTRACT: Address = address!("0000000000000000000000000000000000100004");
    emit_meta(
        "rex6_selfdestruct_shaped",
        "REX6",
        "SSTORE+TIMESTAMP+MUL pad (storage/volatile checkpoints)",
    );
    let mut db = CacheDB::<EmptyDB>::default();
    let mut code = Vec::new();
    for i in 0..80u64 {
        push8(&mut code, i + 1);
        push8(&mut code, i);
        code.push(SSTORE);
        code.extend_from_slice(&[TIMESTAMP, POP]);
        for j in 0..8u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, MUL, POP]);
        }
    }
    code.push(STOP);
    set_code(&mut db, CONTRACT, code.into());
    exec_one(
        "rex6_selfdestruct_shaped",
        0,
        SPEC,
        db,
        CALLER,
        CONTRACT,
        10_000_000_000,
    );
}

fn wl_rex6_eip7702_shaped() {
    emit_meta(
        "rex6_eip7702_shaped",
        "REX6",
        "approx nested CALL depth4 + SSTORE + detention (not full 7702 auth)",
    );
    let a = address!("0000000000000000000000000000000000100011");
    let b = address!("0000000000000000000000000000000000100012");
    let c = address!("0000000000000000000000000000000000100013");
    let d = address!("0000000000000000000000000000000000100014");
    let mut db = CacheDB::<EmptyDB>::default();
    let mut leaf = vec![TIMESTAMP, POP];
    push8(&mut leaf, 7);
    push8(&mut leaf, 0);
    leaf.push(SSTORE);
    leaf.push(STOP);
    set_code(&mut db, d, leaf.into());
    for (this, next) in [(c, d), (b, c), (a, b)] {
        let mut code = Vec::new();
        code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0]);
        push20(&mut code, next);
        code.extend_from_slice(&[PUSH2, 0x40, 0x00, CALL, POP, STOP]);
        set_code(&mut db, this, code.into());
    }
    exec_one(
        "rex6_eip7702_shaped",
        0,
        MegaSpecId::REX6,
        db,
        CALLER,
        a,
        10_000_000_000,
    );
}

fn wl_rex6_system_salt_shaped() {
    emit_meta(
        "rex6_system_salt_shaped",
        "REX6",
        "ordinary-caller SSTORE/SLOAD x200 (system exemption not exercised)",
    );
    let contract = address!("0000000000000000000000000000000000100015");
    let mut db = CacheDB::<EmptyDB>::default();
    let mut code = Vec::new();
    for i in 0..200u64 {
        push8(&mut code, i + 3);
        push8(&mut code, i);
        code.push(SSTORE);
        push8(&mut code, i);
        code.extend_from_slice(&[SLOAD, POP]);
    }
    code.push(STOP);
    set_code(&mut db, contract, code.into());
    exec_one(
        "rex6_system_salt_shaped",
        0,
        MegaSpecId::REX6,
        db,
        CALLER,
        contract,
        10_000_000_000,
    );
}

fn wl_synth_straight_arith() {
    emit_meta(
        "synth_straight_arith",
        "REX4",
        "2000x ADD/MUL/SUB straight-line after TIMESTAMP — V1 long segment",
    );
    let contract = address!("0000000000000000000000000000000000100020");
    let mut db = CacheDB::<EmptyDB>::default();
    let mut code = vec![TIMESTAMP, POP];
    for i in 0..2000u32 {
        let v = (i % 200) as u8;
        code.extend_from_slice(&[PUSH1, v, PUSH1, 3, ADD, PUSH1, 2, MUL, PUSH1, 1, SUB, POP]);
    }
    code.push(STOP);
    set_code(&mut db, contract, code.into());
    exec_one(
        "synth_straight_arith",
        0,
        MegaSpecId::REX4,
        db,
        CALLER,
        contract,
        2_000_000_000,
    );
}

fn wl_synth_jump_loop() {
    emit_meta(
        "synth_jump_loop",
        "REX4",
        "JUMPI loop 500 iters with 4 ADD body",
    );
    let contract = address!("0000000000000000000000000000000000100021");
    let mut db = CacheDB::<EmptyDB>::default();
    let mut code = Vec::new();
    code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, MSTORE]);
    let loop_pc = code.len();
    assert!(loop_pc < 256);
    code.push(JUMPDEST);
    // MLOAD=0x51, DUP1=0x80
    code.extend_from_slice(&[PUSH1, 0, 0x51, PUSH1, 1, ADD, 0x80, PUSH1, 0, MSTORE]);
    for j in 0..4u8 {
        code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
    }
    // EVM LT: a=pop(), b=pop(), push(a<b). Want counter < 500 ⇒ a=counter, b=500
    // stack build: push 500; mload counter; LT
    code.extend_from_slice(&[PUSH2, 0x01, 0xF4, PUSH1, 0, 0x51, LT, PUSH1, loop_pc as u8, JUMPI]);
    code.push(STOP);
    set_code(&mut db, contract, code.into());
    exec_one(
        "synth_jump_loop",
        0,
        MegaSpecId::REX4,
        db,
        CALLER,
        contract,
        2_000_000_000,
    );
}

fn wl_synth_basic_blocks() {
    emit_meta("synth_basic_blocks", "REX4", "100 JUMPDEST blocks x3 ADD");
    let contract = address!("0000000000000000000000000000000000100022");
    let mut db = CacheDB::<EmptyDB>::default();
    let mut code = Vec::new();
    for _ in 0..100u16 {
        code.push(JUMPDEST);
        for j in 0..3u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
        }
    }
    code.push(STOP);
    set_code(&mut db, contract, code.into());
    exec_one(
        "synth_basic_blocks",
        0,
        MegaSpecId::REX4,
        db,
        CALLER,
        contract,
        2_000_000_000,
    );
}

fn wl_synth_nested_calls() {
    emit_meta(
        "synth_nested_calls",
        "REX4",
        "CALL tree depth2 width2 + leaf SSTORE",
    );
    let root = address!("0000000000000000000000000000000000100030");
    let leaves = [
        address!("0000000000000000000000000000000000100031"),
        address!("0000000000000000000000000000000000100032"),
        address!("0000000000000000000000000000000000100033"),
        address!("0000000000000000000000000000000000100034"),
    ];
    let mid1 = address!("0000000000000000000000000000000000100035");
    let mid2 = address!("0000000000000000000000000000000000100036");
    let mut db = CacheDB::<EmptyDB>::default();
    for (i, leaf) in leaves.iter().enumerate() {
        let mut code = Vec::new();
        for j in 0..20u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
        }
        push8(&mut code, i as u64 + 1);
        push8(&mut code, i as u64);
        code.push(SSTORE);
        code.push(STOP);
        set_code(&mut db, *leaf, code.into());
    }
    for (mid, kids) in [(mid1, &leaves[0..2]), (mid2, &leaves[2..4])] {
        let mut code = Vec::new();
        for k in kids {
            code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0]);
            push20(&mut code, *k);
            code.extend_from_slice(&[PUSH2, 0x20, 0x00, CALL, POP]);
        }
        code.push(STOP);
        set_code(&mut db, mid, code.into());
    }
    let mut code = Vec::new();
    for m in [mid1, mid2] {
        code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0, PUSH1, 0]);
        push20(&mut code, m);
        code.extend_from_slice(&[PUSH2, 0x30, 0x00, CALL, POP]);
    }
    code.push(STOP);
    set_code(&mut db, root, code.into());
    exec_one(
        "synth_nested_calls",
        0,
        MegaSpecId::REX4,
        db,
        CALLER,
        root,
        2_000_000_000,
    );
}

fn wl_synth_return_paths() {
    emit_meta(
        "synth_return_paths",
        "REX4",
        "REVERT and RETURN terminators + compute",
    );
    let c_ok = address!("0000000000000000000000000000000000100040");
    let c_rev = address!("0000000000000000000000000000000000100041");
    let mut make = |ret_op: u8| -> Bytes {
        let mut code = vec![TIMESTAMP, POP];
        for j in 0..50u8 {
            code.extend_from_slice(&[PUSH1, j, PUSH1, 1, ADD, POP]);
        }
        code.extend_from_slice(&[PUSH1, 0, PUSH1, 0, ret_op]);
        code.into()
    };
    let mut db1 = CacheDB::<EmptyDB>::default();
    set_code(&mut db1, c_ok, make(RETURN));
    exec_one(
        "synth_return_paths",
        0,
        MegaSpecId::REX4,
        db1,
        CALLER,
        c_ok,
        50_000_000,
    );
    let mut db2 = CacheDB::<EmptyDB>::default();
    set_code(&mut db2, c_rev, make(REVERT));
    exec_one(
        "synth_return_paths",
        1,
        MegaSpecId::REX4,
        db2,
        CALLER,
        c_rev,
        50_000_000,
    );
}

fn main() {
    let only = env::var("WL_ONLY").unwrap_or_default();
    let all = only.is_empty();
    let want = |name: &str| all || only.split(',').any(|x| x.trim() == name);

    println!(
        r#"{{"type":"run_meta","tool":"ckpt_overshoot_probe","rpc":"rate_limited","eest":"not_bundled","note":"probe_suite+synthetic; no megaeth-labs writes"}}"#
    );

    if want("sweep_hotloop_v2") {
        wl_sweep_hotloop_v2();
    }
    if want("rex6_sstore_log") {
        wl_rex6_sstore_log();
    }
    if want("rex6_create_shaped") {
        wl_rex6_create_shaped();
    }
    if want("rex6_selfdestruct_shaped") {
        wl_rex6_selfdestruct_shaped();
    }
    if want("rex6_eip7702_shaped") {
        wl_rex6_eip7702_shaped();
    }
    if want("rex6_system_salt_shaped") {
        wl_rex6_system_salt_shaped();
    }
    if want("synth_straight_arith") {
        wl_synth_straight_arith();
    }
    if want("synth_jump_loop") {
        wl_synth_jump_loop();
    }
    if want("synth_basic_blocks") {
        wl_synth_basic_blocks();
    }
    if want("synth_nested_calls") {
        wl_synth_nested_calls();
    }
    if want("synth_return_paths") {
        wl_synth_return_paths();
    }
}
