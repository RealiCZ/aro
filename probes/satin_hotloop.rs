//! ARO bench probe for the Satin engine (mega-evm, spec `SATIN`): the workloads CodSpeed measures,
//! one row per CodSpeed row, named by the row key `mega-bench-reporter` gives it.
//!
//! The rows are the Satin rows of the crate's four CodSpeed benches (`cargo codspeed build -p
//! mega-evm`), built as those benches build them — the code below is theirs, copied, and the corpus
//! rows load the same scenario files under `benches/scenarios/`:
//!
//! - `transact/<workload>/<arm>` (22 rows): `benches/transact.rs`, every arm but `op_revm`;
//! - `corpus/<id>` (13 rows): `benches/corpus.rs`, its `SLICE`;
//! - `block/{transfers,storage_writes,steady_pre_block}`: `benches/block.rs`;
//! - `factory/{create_evm,create_evm_with_inspector}`: `benches/factory.rs`.
//!
//! The `op_revm` arms (16 rows) are the target's control lanes: they run no mega-evm code, so the
//! probe leaves them out. `probes/satin_rowmap.py` checks that these names are exactly the Satin
//! rows the benches declare.
//!
//! A row's measured region is what CodSpeed measures: the routine alone, after its setup (a fresh
//! EVM, a fresh `State`, a scenario's pre-state) and before its output is dropped, after five
//! unmeasured runs (CodSpeed's `WARMUP_RUNS`). The routine runs inside `aro_probe_measured`, so a
//! callgrind run with `--collect-atstart=no --toggle-collect=aro_probe_measured` counts exactly
//! that region, as CodSpeed's start and end markers do. Every row is checked once before anything
//! is measured, with the bench's own assertions.
//!
//! Modes:
//!   aro_satin_hotloop                 bench: one `ROW` line per row, then 5 samples of ns per round
//!                                     (each row once, corpus rows four times, block rows three
//!                                     times) -> `BENCH s1 .. s5`;
//!                                     rounds per sample scale with ARO_BENCH_SCALE
//!   aro_satin_hotloop <secs>          profile: spin rounds until the deadline -> `SPUN n rounds`
//!   ARO_PROBE_LIST=1                  print `ROWS <row> <row> ...` and exit
//!   ARO_PROBE_ROW=<row> [ARO_PROBE_ITERS=n]
//!                                     build that row alone, run it five times unmeasured, then n
//!                                     times (default 8) through `aro_probe_measured`
//!                                     -> `ROWRAN <row> <n>`

use std::{
    hint::black_box,
    path::Path,
    time::{Duration, Instant},
};

use alloy_consensus::{transaction::Recovered, Signed, TxLegacy};
use alloy_eips::{
    eip2935::{HISTORY_STORAGE_ADDRESS, HISTORY_STORAGE_CODE},
    eip4788::{BEACON_ROOTS_ADDRESS, BEACON_ROOTS_CODE},
};
use alloy_evm::{block::BlockExecutor, EvmEnv, EvmFactory};
use alloy_op_evm::{block::receipt_builder::OpAlloyReceiptBuilder, OpTx};
use alloy_primitives::{address, Address, Bytes, Signature, TxKind, B256, U160, U256};
use alloy_sol_types::SolCall;
use mega_evm::{
    constants::COST_PER_HISTORY_BYTE,
    system::{
        keyless::{
            decode_keyless_tx,
            tests::{CREATE2_FACTORY_CODE_HASH, CREATE2_FACTORY_CONTRACT, CREATE2_FACTORY_TX},
            IKeylessDeploy, KEYLESS_DEPLOY_ADDRESS, KEYLESS_DEPLOY_CODE,
        },
        IMegaAccessControl, IMegaLimitControl, ACCESS_CONTROL_ADDRESS, ACCESS_CONTROL_CODE,
        LIMIT_CONTROL_ADDRESS, LIMIT_CONTROL_CODE, MEGA_SYSTEM_ADDRESS, ORACLE_CONTRACT_ADDRESS,
    },
    test_utils::{
        is_transfer_log, op_transaction, zero_fee_l1_block_info, BytecodeBuilder, CommittedStates,
        MemoryDatabase, Scenario,
    },
    BlockLimits, EvmTxRuntimeLimits, ExternalEnvs, LimitCheck, LimitKind, LimitUsage,
    MegaBlockExecutionCtx, MegaBlockExecutor, MegaContext, MegaEvm, MegaEvmFactory,
    MegaHardforkConfig, MegaSpecId, MegaTxEnvelope, TestExternalEnvs, VolatileDataAccess,
    LOG_BASE_SIZE, LOG_TOPIC_SIZE, MIN_BUCKET_SIZE, TRANSFER_LOG_SIZE, TX_BODY_SIZE,
    WRITE_RECORD_SIZE,
};
use op_revm::OpTransaction;
use revm::{
    bytecode::opcode::{
        ADDRESS, BALANCE, CALL, CALLDATACOPY, CALLDATALOAD, CODECOPY, DUP1, EXP, GAS, ISZERO,
        JUMPDEST, JUMPI, KECCAK256, LOG0, LOG2, MCOPY, MSTORE, NUMBER, POP, PUSH0, PUSH1,
        RETURNDATACOPY, SLOAD, SSTORE, STATICCALL, STOP, SUB, SWAP1, TIMESTAMP,
    },
    context::{BlockEnv, CfgEnv, TxEnv},
    database::{EmptyDB, State},
    inspector::NoOpInspector,
    state::EvmState,
    DatabaseCommit, ExecuteEvm,
};

// ---- the measured region ------------------------------------------------------------------

/// The one function a per-row instruction count collects (see the module docs).
#[no_mangle]
#[inline(never)]
fn aro_probe_measured(routine: &mut dyn FnMut()) {
    routine();
}

/// Unmeasured runs before the measured ones, as CodSpeed's `WARMUP_RUNS`.
const WARMUP_RUNS: u32 = 5;

/// One run of a row: its setup, then its routine through `aro_probe_measured`; returns the time
/// the routine took.
type Runner = Box<dyn FnMut() -> Duration>;

/// `iter_batched`: `setup` outside the measured region, `routine` inside, the output dropped
/// after it.
fn batched<I: 'static, O: 'static>(
    mut setup: impl FnMut() -> I + 'static,
    mut routine: impl FnMut(I) -> O + 'static,
) -> Runner {
    Box::new(move || {
        let mut input = Some(black_box(setup()));
        let mut output = None;
        let start = Instant::now();
        aro_probe_measured(&mut || output = Some(routine(input.take().expect("one input"))));
        let elapsed = start.elapsed();
        drop(black_box(output));
        elapsed
    })
}

/// `iter`: the routine and the drop of its output inside the measured region.
fn plain<O: 'static>(mut routine: impl FnMut() -> O + 'static) -> Runner {
    Box::new(move || {
        let start = Instant::now();
        aro_probe_measured(&mut || {
            black_box(routine());
        });
        start.elapsed()
    })
}

struct Row {
    name: String,
    /// Runs per bench round.
    weight: u32,
    run: Runner,
}

fn row(name: impl Into<String>, weight: u32, run: Runner) -> Row {
    Row { name: name.into(), weight, run }
}

// ---- transact (benches/transact.rs) -------------------------------------------------------

const CALLER: Address = address!("0x0000000000000000000000000000000000100000");
const CALLEE: Address = address!("0x0000000000000000000000000000000000100001");
const RECURSIVE: Address = address!("0x0000000000000000000000000000000000100002");
const WRITER: Address = address!("0x0000000000000000000000000000000000100003");
const LOGGER: Address = address!("0x0000000000000000000000000000000000100004");
const SALT_WRITER: Address = address!("0x0000000000000000000000000000000000100005");
const SALT_CALLER: Address = address!("0x0000000000000000000000000000000000100006");
const INTERCEPTED: Address = address!("0x0000000000000000000000000000000000100007");
const MISSING: Address = address!("0x0000000000000000000000000000000000100008");
const LIMITED: Address = address!("0x0000000000000000000000000000000000100009");
const READER: Address = address!("0x000000000000000000000000000000000010000a");
const VOLATILE: Address = address!("0x000000000000000000000000000000000010000b");
const HASHER: Address = address!("0x000000000000000000000000000000000010000c");
const VALUE_CALLER: Address = address!("0x000000000000000000000000000000000010000d");
const QUERIER: Address = address!("0x000000000000000000000000000000000010000e");
const BENEFICIARY: Address = Address::ZERO;
const IS_DISABLED: [u8; 4] = IMegaAccessControl::isVolatileDataAccessDisabledCall::SELECTOR;
const UNKNOWN_SELECTOR: [u8; 4] = [0xde, 0xad, 0xbe, 0xef];
const REMAINING_COMPUTE_GAS: [u8; 4] = IMegaLimitControl::remainingComputeGasCall::SELECTOR;
const DEPTH: u8 = 64;
const REPEAT: u64 = 200;
const CALLDATA_LEN: usize = 4 * 1024;
const DONE: u8 = 0x1f;
const SALT_REPEAT: u64 = 16;
const SALT_MULTIPLIER: u64 = 8;
const SALT_GAS_LIMIT: u64 = 60_000_000;
const SALT_ACCOUNT_BASE: u64 = 0x200000;
const LIMITED_KEPT: u64 =
    TX_BODY_SIZE + REPEAT * WRITE_RECORD_SIZE + REPEAT * (LOG_BASE_SIZE + 2 * LOG_TOPIC_SIZE + 32);
const LIMITED_GAS_LIMIT: u64 = 60_000_000;

/// The 14 workloads with a `satin` and an `op_revm` arm, in the bench's order.
const TRANSACT_WORKLOADS: &[&str] = &[
    "empty_transaction",
    "ether_transfer",
    "deep_calls",
    "storage_writes",
    "logs",
    "calldata",
    "intercepted_calls",
    "system_address_misses",
    "remaining_compute_gas",
    "value_calls",
    "storage_reads",
    "volatile_reads",
    "hashes_and_copies",
    "oracle_reads",
];
/// The rows `MegaEvm` alone runs, or that have more arms than `satin`.
const TRANSACT_EXTRA: &[&str] = &[
    "data_size_limit/satin",
    "data_size_limit/stopped",
    "oracle_reads/service",
    "keyless_deploy/satin",
    "salt_storage_writes/satin",
    "salt_storage_writes/crowded",
    "salt_new_accounts/satin",
    "salt_new_accounts/crowded",
];

fn recursive_code() -> Bytes {
    #[rustfmt::skip]
    let body = [
        PUSH0, CALLDATALOAD,
        DUP1, ISZERO, PUSH1, DONE, JUMPI,
        DUP1, PUSH1, 1, SWAP1, SSTORE,
        PUSH0, PUSH0, LOG0,
        PUSH1, 1, SWAP1, SUB, PUSH0, MSTORE,
        PUSH0, PUSH0, PUSH1, 0x20, PUSH0, PUSH0, ADDRESS, GAS, CALL,
        STOP,
        JUMPDEST, STOP,
    ];
    assert_eq!(body[DONE as usize], JUMPDEST);
    Bytes::from(body.to_vec())
}

fn writer_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for slot in 0..REPEAT {
        code = code.sstore(U256::from(slot), U256::from(1));
    }
    for slot in 0..REPEAT {
        code = code.sstore(U256::from(slot), U256::ZERO);
    }
    code.stop().build()
}

fn logger_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for topic in 0..REPEAT {
        code = code
            .push_number(topic)
            .push_number(topic)
            .push_number(32_u8)
            .append(PUSH0)
            .append(LOG2);
    }
    code.stop().build()
}

fn reader_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for slot in 0..REPEAT {
        code = code.push_number(slot).append(SLOAD).append(POP);
    }
    code.stop().build()
}

fn hasher_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for _ in 0..REPEAT {
        code = code
            .push_number(32_u8)
            .append_many([PUSH0, KECCAK256, POP])
            .push_number(32_u8)
            .append_many([PUSH0, PUSH0, MCOPY])
            .push_number(32_u8)
            .append_many([PUSH0, PUSH0, CALLDATACOPY])
            .push_number(32_u8)
            .append_many([PUSH0, PUSH0, CODECOPY])
            .append_many([PUSH0, PUSH0, PUSH0, RETURNDATACOPY])
            .push_number(0xff_u8)
            .push_number(3_u8)
            .append_many([EXP, POP]);
    }
    code.stop().build()
}

fn volatile_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for _ in 0..REPEAT {
        code = code
            .append_many([TIMESTAMP, POP, NUMBER, POP])
            .push_address(BENEFICIARY)
            .append(BALANCE)
            .append(POP);
    }
    code.append_many([PUSH0, PUSH0, PUSH0, PUSH0, PUSH0])
        .push_address(ORACLE_CONTRACT_ADDRESS)
        .append(GAS)
        .append(CALL)
        .append(POP)
        .stop()
        .build()
}

fn limited_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for slot in 0..REPEAT {
        code = code.sstore(U256::from(slot), U256::from(1));
    }
    for topic in 0..REPEAT {
        code = code
            .push_number(topic)
            .push_number(topic)
            .push_number(32_u8)
            .append(PUSH0)
            .append(LOG2);
    }
    code.stop().build()
}

fn salt_writer_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for slot in 0..SALT_REPEAT {
        code = code.sstore(U256::from(slot), U256::from(1));
    }
    code.stop().build()
}

fn salt_caller_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for i in 0..SALT_REPEAT {
        code = code
            .push_number(0_u64)
            .push_number(0_u64)
            .push_number(0_u64)
            .push_number(0_u64)
            .push_number(1_u64)
            .push_address(Address::from(U160::from(SALT_ACCOUNT_BASE + i)))
            .push_number(1_000_000_u64)
            .append(CALL)
            .append(POP);
    }
    code.stop().build()
}

fn value_caller_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for _ in 0..REPEAT {
        code = code.call(CALLEE, U256::from(1)).append(POP);
    }
    code.stop().build()
}

fn salt_envs(m: u64) -> TestExternalEnvs {
    TestExternalEnvs::new().with_default_bucket_capacity(MIN_BUCKET_SIZE as u64 * m)
}

fn salt_context(
    db: MemoryDatabase,
    envs: TestExternalEnvs,
) -> MegaContext<MemoryDatabase, TestExternalEnvs> {
    MegaContext::new_with_external_envs(db, MegaSpecId::SATIN, ExternalEnvs::from(envs))
        .with_chain(zero_fee_l1_block_info())
}

fn system_caller_code(target: Address, selector: [u8; 4]) -> Bytes {
    let mut code = BytecodeBuilder::default().mstore(0x0, selector);
    for _ in 0..REPEAT {
        code = code
            .push_number(0_u64)
            .push_number(0_u64)
            .push_number(4_u64)
            .push_number(0_u64)
            .push_address(target)
            .push_number(100_000_u64)
            .append(STATICCALL)
            .append(POP);
    }
    code.stop().build()
}

fn call_tx(to: Address, data: Bytes, gas_limit: u64) -> OpTransaction<TxEnv> {
    op_transaction(TxEnv {
        caller: CALLER,
        kind: TxKind::Call(to),
        data,
        gas_limit,
        ..Default::default()
    })
}

fn tx(value: U256) -> OpTransaction<TxEnv> {
    op_transaction(TxEnv {
        caller: CALLER,
        kind: TxKind::Call(CALLEE),
        value,
        gas_limit: 1_000_000,
        ..Default::default()
    })
}

fn mega_context(db: MemoryDatabase) -> MegaContext<MemoryDatabase> {
    MegaContext::new(db, MegaSpecId::SATIN).with_chain(zero_fee_l1_block_info())
}

fn transact_database() -> MemoryDatabase {
    MemoryDatabase::default()
        .account_balance(CALLER, U256::from(10u64.pow(18)))
        .account_balance(CALLEE, U256::from(1))
        .account_code(RECURSIVE, recursive_code())
        .account_code(WRITER, writer_code())
        .account_code(LOGGER, logger_code())
        .account_code(SALT_WRITER, salt_writer_code())
        .account_balance(SALT_CALLER, U256::from(10u64.pow(9)))
        .account_code(SALT_CALLER, salt_caller_code())
        .account_code(INTERCEPTED, system_caller_code(ACCESS_CONTROL_ADDRESS, IS_DISABLED))
        .account_code(MISSING, system_caller_code(ACCESS_CONTROL_ADDRESS, UNKNOWN_SELECTOR))
        .account_code(QUERIER, system_caller_code(LIMIT_CONTROL_ADDRESS, REMAINING_COMPUTE_GAS))
        .account_code(LIMITED, limited_code())
        .account_balance(VALUE_CALLER, U256::from(REPEAT))
        .account_code(VALUE_CALLER, value_caller_code())
        .account_code(READER, reader_code())
        .account_code(VOLATILE, volatile_code())
        .account_code(HASHER, hasher_code())
        .account_code(ORACLE_CONTRACT_ADDRESS, reader_code())
        .account_code(ACCESS_CONTROL_ADDRESS, ACCESS_CONTROL_CODE)
        .account_code(LIMIT_CONTROL_ADDRESS, LIMIT_CONTROL_CODE)
        .account_code(KEYLESS_DEPLOY_ADDRESS, KEYLESS_DEPLOY_CODE)
}

fn workload_tx(workload: &str) -> OpTransaction<TxEnv> {
    let mut depth = [0u8; 32];
    depth[31] = DEPTH;
    match workload {
        "empty_transaction" => tx(U256::ZERO),
        "ether_transfer" => tx(U256::from(1)),
        "deep_calls" => call_tx(RECURSIVE, Bytes::from(depth.to_vec()), 30_000_000),
        "storage_writes" => call_tx(WRITER, Bytes::new(), 30_000_000),
        "logs" => call_tx(LOGGER, Bytes::new(), 30_000_000),
        "calldata" => call_tx(CALLEE, Bytes::from(vec![0xab_u8; CALLDATA_LEN]), 30_000_000),
        "intercepted_calls" => call_tx(INTERCEPTED, Bytes::new(), 30_000_000),
        "system_address_misses" => call_tx(MISSING, Bytes::new(), 30_000_000),
        "remaining_compute_gas" => call_tx(QUERIER, Bytes::new(), 30_000_000),
        "value_calls" => call_tx(VALUE_CALLER, Bytes::new(), 30_000_000),
        "storage_reads" => call_tx(READER, Bytes::new(), 30_000_000),
        "volatile_reads" => call_tx(VOLATILE, Bytes::new(), 30_000_000),
        "hashes_and_copies" => call_tx(HASHER, Bytes::from(vec![0xab_u8; 32]), 30_000_000),
        "oracle_reads" => call_tx(ORACLE_CONTRACT_ADDRESS, Bytes::new(), 30_000_000),
        other => panic!("no transact workload {other}"),
    }
}

/// The bench's checks on one workload's Satin run.
fn check_workload(workload: &str, db: &MemoryDatabase, tx: &OpTransaction<TxEnv>) {
    let mut evm = MegaEvm::new(mega_context(db.clone()));
    let satin = evm.execute_transaction(OpTx(tx.clone())).unwrap();
    assert!(satin.result.is_success(), "{workload}: {:?}", satin.result);
    let detention = evm.ctx().detention();
    if workload == "oracle_reads" {
        assert_eq!(detention.accessed(), VolatileDataAccess::ORACLE, "oracle_reads: marked");
        assert!(detention.compute_limit().is_some(), "oracle_reads: the reads must detain");
    } else if workload == "volatile_reads" {
        assert_eq!(
            detention.accessed(),
            VolatileDataAccess::TIMESTAMP |
                VolatileDataAccess::BLOCK_NUMBER |
                VolatileDataAccess::BENEFICIARY_BALANCE |
                VolatileDataAccess::ORACLE,
            "volatile_reads: every read must be marked",
        );
        assert!(detention.compute_limit().is_some(), "volatile_reads: the reads must detain");
    } else {
        assert_eq!(detention.compute_limit(), None, "{workload}: nothing here is volatile");
    }
    let body = TX_BODY_SIZE + tx.base.data.len() as u64;
    assert!(satin.gas.history >= body * COST_PER_HISTORY_BYTE, "{workload}: pays for its body");
    match workload {
        "deep_calls" => {
            let written =
                satin.state[&RECURSIVE].storage.values().filter(|s| s.is_changed()).count();
            assert_eq!(written, DEPTH as usize, "every frame of the recursion ran");
        }
        "logs" => assert_eq!(
            satin.gas.history,
            (TX_BODY_SIZE + REPEAT * (LOG_BASE_SIZE + 2 * LOG_TOPIC_SIZE + 32)) *
                COST_PER_HISTORY_BYTE,
            "the logs must draw history gas",
        ),
        "calldata" => assert_eq!(
            satin.gas.history,
            body * COST_PER_HISTORY_BYTE,
            "the calldata must draw history gas"
        ),
        "value_calls" => {
            let logs = satin.result.logs().iter().filter(|log| is_transfer_log(log)).count();
            assert_eq!(logs, REPEAT as usize, "every call moved its wei and logged it");
            assert_eq!(
                satin.usage.data_size - satin.usage.write_records * WRITE_RECORD_SIZE,
                body + REPEAT * TRANSFER_LOG_SIZE,
                "every transfer log was counted",
            );
        }
        _ => {}
    }
}

/// The transact rows whose names `want` accepts, checked as the bench checks them.
fn transact_rows(want: &dyn Fn(&str) -> bool) -> Vec<Row> {
    let mut rows = Vec::new();
    if !transact_names().iter().any(|n| want(n)) {
        return rows;
    }
    let db = transact_database();
    for workload in TRANSACT_WORKLOADS {
        let name = format!("transact/{workload}/satin");
        if !want(&name) {
            continue;
        }
        let tx = workload_tx(workload);
        check_workload(workload, &db, &tx);
        let db = db.clone();
        rows.push(row(
            name,
            1,
            batched(
                move || MegaEvm::new(mega_context(db.clone())),
                move |mut evm| evm.transact(OpTx(tx.clone())).unwrap(),
            ),
        ));
    }

    // The data-size arms: a limit the transaction exactly reaches, and one a byte short of it.
    let limited_tx = call_tx(LIMITED, Bytes::new(), LIMITED_GAS_LIMIT);
    let limited = {
        let db = db.clone();
        move |limit| {
            mega_context(db.clone()).with_tx_runtime_limits(
                EvmTxRuntimeLimits::default().with_tx_data_size_limit(limit),
            )
        }
    };
    if want("transact/data_size_limit/satin") || want("transact/data_size_limit/stopped") {
        let at_limit = MegaEvm::new(limited(LIMITED_KEPT))
            .execute_transaction(OpTx(limited_tx.clone()))
            .unwrap();
        assert!(at_limit.result.is_success(), "data_size_limit: {:?}", at_limit.result);
        assert_eq!(
            at_limit.usage,
            LimitUsage { data_size: LIMITED_KEPT, write_records: REPEAT, state_growth: REPEAT },
            "data_size_limit: keeps every byte and record it is sized for",
        );
        let stopped = MegaEvm::new(limited(LIMITED_KEPT - 1))
            .execute_transaction(OpTx(limited_tx.clone()))
            .unwrap();
        let body = TX_BODY_SIZE + limited_tx.base.data.len() as u64;
        assert_eq!(
            stopped.limit_exceeded,
            Some(LimitCheck::ExceedsLimit {
                kind: LimitKind::DataSize,
                limit: LIMITED_KEPT - 1 - body,
                used: LIMITED_KEPT - body,
                frame_local: true,
            }),
            "data_size_limit/stopped: the last log must be the byte that crosses",
        );
    }
    for (arm, limit) in [("satin", LIMITED_KEPT), ("stopped", LIMITED_KEPT - 1)] {
        let name = format!("transact/data_size_limit/{arm}");
        if !want(&name) {
            continue;
        }
        let (limited, tx) = (limited.clone(), limited_tx.clone());
        rows.push(row(
            name,
            1,
            batched(
                move || MegaEvm::new(limited(limit)),
                move |mut evm| evm.transact(OpTx(tx.clone())).unwrap(),
            ),
        ));
    }

    // The Oracle's storage answered by the oracle service, every slot of it.
    if want("transact/oracle_reads/service") {
        let service = (0..REPEAT).fold(TestExternalEnvs::new(), |envs, slot| {
            envs.with_oracle_storage(U256::from(slot), U256::ONE)
        });
        let oracle_tx = call_tx(ORACLE_CONTRACT_ADDRESS, Bytes::new(), 30_000_000);
        let from_service = MegaEvm::new(salt_context(db.clone(), service.clone()))
            .execute_transaction(OpTx(oracle_tx.clone()))
            .unwrap();
        let from_state = MegaEvm::new(mega_context(db.clone()))
            .execute_transaction(OpTx(oracle_tx.clone()))
            .unwrap();
        assert!(from_service.result.is_success(), "oracle_reads/service: {:?}", from_service.result);
        assert_eq!(from_service.gas, from_state.gas, "oracle_reads/service: priced as the database's");
        let db = db.clone();
        rows.push(row(
            "transact/oracle_reads/service",
            1,
            batched(
                move || MegaEvm::new(salt_context(db.clone(), service.clone())),
                move |mut evm| evm.transact(OpTx(oracle_tx.clone())).unwrap(),
            ),
        ));
    }

    // The keyless arm: the canonical CREATE2 factory through `keylessDeploy`.
    if want("transact/keyless_deploy/satin") {
        let keyless_tx = call_tx(
            KEYLESS_DEPLOY_ADDRESS,
            IKeylessDeploy::keylessDeployCall {
                keylessDeploymentTransaction: Bytes::from_static(CREATE2_FACTORY_TX),
                gasLimitOverride: U256::from(10_000_000),
            }
            .abi_encode()
            .into(),
            30_000_000,
        );
        let deployed = MegaEvm::new(mega_context(db.clone()))
            .execute_transaction(OpTx(keyless_tx.clone()))
            .unwrap();
        let output = deployed.result.output().cloned().unwrap_or_default();
        let answer = IKeylessDeploy::keylessDeployCall::abi_decode_returns(&output)
            .expect("keyless_deploy: answers in the keylessDeploy ABI");
        assert_eq!(answer.deployedAddress, CREATE2_FACTORY_CONTRACT, "keyless_deploy: deployed");
        assert_eq!(
            deployed.state[&CREATE2_FACTORY_CONTRACT].info.code_hash, CREATE2_FACTORY_CODE_HASH,
            "keyless_deploy: the factory's code is what it deployed",
        );
        assert!(decode_keyless_tx(CREATE2_FACTORY_TX).is_ok());
        let db = db.clone();
        rows.push(row(
            "transact/keyless_deploy/satin",
            1,
            batched(
                move || MegaEvm::new(mega_context(db.clone())),
                move |mut evm| evm.transact(OpTx(keyless_tx.clone())).unwrap(),
            ),
        ));
    }

    // The SALT arms: a minimal environment and a crowded one.
    for (workload, to) in [("salt_storage_writes", SALT_WRITER), ("salt_new_accounts", SALT_CALLER)]
    {
        let tx = call_tx(to, Bytes::new(), SALT_GAS_LIMIT);
        if !want(&format!("transact/{workload}/satin")) &&
            !want(&format!("transact/{workload}/crowded"))
        {
            continue;
        }
        let (minimal, crowded) = (salt_envs(1), salt_envs(SALT_MULTIPLIER));
        let at_minimum = MegaEvm::new(salt_context(db.clone(), minimal.clone()))
            .execute_transaction(OpTx(tx.clone()))
            .unwrap();
        let at_crowded = MegaEvm::new(salt_context(db.clone(), crowded.clone()))
            .execute_transaction(OpTx(tx.clone()))
            .unwrap();
        assert!(at_minimum.result.is_success(), "{workload}: {:?}", at_minimum.result);
        assert!(at_crowded.result.is_success(), "{workload}: {:?}", at_crowded.result);
        assert_eq!(
            at_crowded.gas.state,
            at_minimum.gas.state * SALT_MULTIPLIER,
            "{workload}: the crowded arm pays the crowded price",
        );
        for (arm, envs) in [("satin", minimal), ("crowded", crowded)] {
            let name = format!("transact/{workload}/{arm}");
            if !want(&name) {
                continue;
            }
            let (db, tx) = (db.clone(), tx.clone());
            rows.push(row(
                name,
                1,
                batched(
                    move || MegaEvm::new(salt_context(db.clone(), envs.clone())),
                    move |mut evm| evm.transact(OpTx(tx.clone())).unwrap(),
                ),
            ));
        }
    }
    rows
}

fn transact_names() -> Vec<String> {
    TRANSACT_WORKLOADS
        .iter()
        .map(|w| format!("transact/{w}/satin"))
        .chain(TRANSACT_EXTRA.iter().map(|r| format!("transact/{r}")))
        .collect()
}

// ---- corpus (benches/corpus.rs) -----------------------------------------------------------

/// `(bench id, scenario name)`: the scenario is `benches/scenarios/<name>.json`.
const SLICE: &[(&str, &str)] = &[
    ("sstore_set", "sstore_set_spills"),
    ("create", "create_success"),
    ("nested_revert", "nested_call_revert_with_spill"),
    ("two_txs", "two_txs_sequential"),
    ("system_call", "system_call"),
    ("calldata_floor", "test_calldata_floor_with_sstore_fork_Amsterdam_state_test"),
    (
        "nested_refund_depth_10",
        "test_nested_state_gas_refund_consumed_at_depth_fork_Amsterdam_state_test_consume_deepest_depth_10_sstore_restoration",
    ),
    ("eip7702_delegation", "eip7702_delegate_new_authority"),
    ("access_list", "access_list_warm_slots_and_account"),
    ("selfdestruct", "selfdestruct_created_in_same_tx_to_existing_account"),
    ("logs", "logs_all_topic_counts"),
    ("ecrecover", "precompile_ecrecover"),
    ("modexp", "precompile_modexp"),
];
/// Runs per bench round of each corpus scenario and of each block row: with these the realistic
/// rows (corpus and block) are about two thirds of a round, so the profile's frontier is theirs.
/// Weights shape the bench round and the profile only; a per-row count runs one row.
const CORPUS_WEIGHT: u32 = 4;
const BLOCK_WEIGHT: u32 = 3;

fn load(name: &str) -> Scenario {
    let file =
        Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("benches/scenarios/{name}.json"));
    let text = std::fs::read_to_string(&file).unwrap_or_else(|err| panic!("{file:?}: {err}"));
    let scenario: Scenario =
        serde_json::from_str(&text).unwrap_or_else(|err| panic!("{file:?}: {err}"));
    scenario.validate().unwrap_or_else(|err| panic!("{file:?}: {err}"));
    scenario
}

fn corpus_rows(want: &dyn Fn(&str) -> bool) -> Vec<Row> {
    let mut rows = Vec::new();
    for (id, name) in SLICE {
        let row_name = format!("corpus/{id}");
        if !want(&row_name) {
            continue;
        }
        let scenario = load(name);
        let outcomes = scenario.run_on(&mut scenario.evm(scenario.database()));
        assert_eq!(outcomes.len(), scenario.txs.len(), "corpus/{id}: every transaction ran");
        let setup = scenario.clone();
        rows.push(row(
            row_name,
            CORPUS_WEIGHT,
            batched(move || setup.evm(setup.database()), move |mut evm| scenario.run_on(&mut evm)),
        ));
    }
    rows
}

// ---- block (benches/block.rs) -------------------------------------------------------------

const N: u64 = 64;
const CHAIN_ID: u64 = 4_326;
const BLOCK_NUMBER: u64 = 1_000;
const BLOCK_TIMESTAMP: u64 = 1_800_000_000;
const PARENT_HASH: B256 = B256::repeat_byte(0xab);
const PARENT_BEACON_ROOT: B256 = B256::repeat_byte(0xcd);
const SENDER: Address = address!("0x2000000000000000000000000000000000000002");
const RECIPIENT: Address = address!("0x3000000000000000000000000000000000000003");
const BLOCK_WRITER: Address = address!("0x1000000000000000000000000000000000000001");

fn block_writer_code() -> Bytes {
    BytecodeBuilder::default()
        .push_number(1_u64)
        .push_number(0_u64)
        .append(CALLDATALOAD)
        .append(SSTORE)
        .stop()
        .build()
}

fn slot_calldata(nonce: u64) -> Bytes {
    Bytes::from(U256::from(nonce + 1).to_be_bytes::<32>())
}

fn block_database() -> MemoryDatabase {
    let mut db = MemoryDatabase::default().rex6_predeploys(MEGA_SYSTEM_ADDRESS);
    db.set_account_code(BLOCK_WRITER, block_writer_code());
    db.set_account_code(RECIPIENT, Bytes::new());
    db.set_account_balance(SENDER, U256::from(1_000_000_000_000_000_u64));
    db.set_account_code(HISTORY_STORAGE_ADDRESS, HISTORY_STORAGE_CODE.clone());
    db.set_account_code(BEACON_ROOTS_ADDRESS, BEACON_ROOTS_CODE.clone());
    db
}

fn evm_env_at(number: u64) -> EvmEnv<MegaSpecId> {
    let mut cfg_env = CfgEnv::new_with_spec(MegaSpecId::SATIN);
    cfg_env.chain_id = CHAIN_ID;
    EvmEnv {
        cfg_env,
        block_env: BlockEnv {
            number: U256::from(number),
            timestamp: U256::from(BLOCK_TIMESTAMP + number - BLOCK_NUMBER),
            gas_limit: 1_000_000_000,
            ..Default::default()
        },
    }
}

fn block_evm_env() -> EvmEnv<MegaSpecId> {
    evm_env_at(BLOCK_NUMBER)
}

fn block_ctx() -> MegaBlockExecutionCtx {
    MegaBlockExecutionCtx::new(
        PARENT_HASH,
        Some(PARENT_BEACON_ROOT),
        Bytes::new(),
        BlockLimits::default(),
    )
}

fn transactions(
    to: Address,
    value: u64,
    gas_limit: u64,
    input: impl Fn(u64) -> Bytes,
) -> Vec<Recovered<MegaTxEnvelope>> {
    (0..N)
        .map(|nonce| {
            let tx = TxLegacy {
                chain_id: Some(CHAIN_ID),
                nonce,
                gas_price: 0,
                gas_limit,
                to: TxKind::Call(to),
                value: U256::from(value),
                input: input(nonce),
            };
            let signed =
                Signed::new_unchecked(tx, Signature::test_signature(), B256::repeat_byte(1));
            Recovered::new_unchecked(MegaTxEnvelope::Legacy(signed), SENDER)
        })
        .collect()
}

fn chain_spec() -> MegaHardforkConfig {
    MegaHardforkConfig::default().with_all_activated()
}

fn pre_block_states(db: MemoryDatabase, number: u64) -> Vec<EvmState> {
    let spec = chain_spec();
    let mut state = State::builder().with_database(db).build();
    let committed = CommittedStates::install(&mut state);
    let evm = MegaEvmFactory::new().create_evm(&mut state, evm_env_at(number));
    let mut executor =
        MegaBlockExecutor::new(evm, block_ctx(), &spec, OpAlloyReceiptBuilder::default());
    executor.apply_pre_execution_changes().expect("the pre-block changes apply");
    drop(executor);
    committed.states()
}

fn steady_database() -> MemoryDatabase {
    let mut db = block_database();
    for state in pre_block_states(db.clone(), BLOCK_NUMBER - 1) {
        db.commit(state);
    }
    db
}

/// The bench's check that the block is a steady one: the two pre-block calls, then the reads of
/// the registry and of the L1 block info, and no read of a predeploy.
fn assert_steady_pre_block(db: &MemoryDatabase) {
    use mega_evm::system::SEQUENCER_REGISTRY_ADDRESS;
    use op_revm::constants::L1_BLOCK_CONTRACT;
    let states = pre_block_states(db.clone(), BLOCK_NUMBER);
    let named: Vec<_> = states
        .iter()
        .map(|state| {
            assert_eq!(state.len(), 1, "a pre-block step names one account: {state:?}");
            *state.keys().next().expect("one account")
        })
        .collect();
    assert_eq!(
        named,
        [HISTORY_STORAGE_ADDRESS, BEACON_ROOTS_ADDRESS, SEQUENCER_REGISTRY_ADDRESS, L1_BLOCK_CONTRACT],
        "a steady block reads no predeploy"
    );
}

fn block_rows(want: &dyn Fn(&str) -> bool) -> Vec<Row> {
    let mut rows = Vec::new();
    let names = ["block/transfers", "block/storage_writes", "block/steady_pre_block"];
    if !names.iter().any(|n| want(n)) {
        return rows;
    }
    let steady = steady_database();
    assert_steady_pre_block(&steady);
    let workloads = [
        ("transfers", transactions(RECIPIENT, 1, 300_000, |_| Bytes::new())),
        ("storage_writes", transactions(BLOCK_WRITER, 0, 300_000, slot_calldata)),
    ];
    for (name, txs) in workloads {
        let row_name = format!("block/{name}");
        if !want(&row_name) {
            continue;
        }
        let spec = chain_spec();
        {
            let mut state = State::builder().with_database(steady.clone()).build();
            let evm = MegaEvmFactory::new().create_evm(&mut state, block_evm_env());
            let mut executor =
                MegaBlockExecutor::new(evm, block_ctx(), &spec, OpAlloyReceiptBuilder::default());
            executor.apply_pre_execution_changes().expect("the block starts");
            for tx in &txs {
                let outcome =
                    executor.execute_transaction_without_commit(tx).expect("the transaction runs");
                assert!(outcome.result.is_success(), "{:?}", outcome.result);
                assert_eq!(outcome.limit_exceeded, None, "no limit stops the workload");
                assert!(outcome.usage.write_records > 0, "every transaction keeps a record");
                executor.commit_transaction(outcome);
            }
            let (_, result) = executor.finish_with_counters().expect("the block finishes");
            assert_eq!(result.receipts().len(), N as usize);
            assert_eq!(result.usage.write_records, N, "one record per transaction, at least");
        }
        let steady = steady.clone();
        rows.push(row(
            row_name,
            BLOCK_WEIGHT,
            batched(
                move || State::builder().with_database(steady.clone()).build(),
                move |mut state| {
                    let result = {
                        let evm = MegaEvmFactory::new().create_evm(&mut state, block_evm_env());
                        let mut executor = MegaBlockExecutor::new(
                            evm,
                            block_ctx(),
                            &spec,
                            OpAlloyReceiptBuilder::default(),
                        );
                        executor.apply_pre_execution_changes().unwrap();
                        for tx in &txs {
                            black_box(executor.execute_transaction(tx).unwrap());
                        }
                        executor.finish_with_counters().unwrap().1
                    };
                    (state, black_box(result))
                },
            ),
        ));
    }
    if want("block/steady_pre_block") {
        let spec = chain_spec();
        rows.push(row(
            "block/steady_pre_block",
            BLOCK_WEIGHT,
            batched(
                move || State::builder().with_database(steady.clone()).build(),
                move |mut state| {
                    let used = {
                        let evm = MegaEvmFactory::new().create_evm(&mut state, block_evm_env());
                        let mut executor = MegaBlockExecutor::new(
                            evm,
                            block_ctx(),
                            &spec,
                            OpAlloyReceiptBuilder::default(),
                        );
                        executor.apply_pre_execution_changes().unwrap();
                        executor.limiter().block_gas_used
                    };
                    (state, black_box(used))
                },
            ),
        ));
    }
    rows
}

// ---- factory (benches/factory.rs) ---------------------------------------------------------

fn factory_env() -> EvmEnv<MegaSpecId> {
    EvmEnv { cfg_env: CfgEnv::new_with_spec(MegaSpecId::SATIN), block_env: BlockEnv::default() }
}

fn factory_rows(want: &dyn Fn(&str) -> bool) -> Vec<Row> {
    let mut rows = Vec::new();
    if want("factory/create_evm") {
        let factory = MegaEvmFactory::new();
        rows.push(row(
            "factory/create_evm",
            1,
            plain(move || drop(black_box(factory.create_evm(EmptyDB::default(), black_box(factory_env()))))),
        ));
    }
    if want("factory/create_evm_with_inspector") {
        let factory = MegaEvmFactory::new();
        rows.push(row(
            "factory/create_evm_with_inspector",
            1,
            plain(move || {
                drop(black_box(factory.create_evm_with_inspector(
                    EmptyDB::default(),
                    black_box(factory_env()),
                    NoOpInspector,
                )))
            }),
        ));
    }
    rows
}

// ---- the probe ----------------------------------------------------------------------------

/// Every row's name, in the order a round runs them: the CodSpeed row keys of the Satin rows.
fn row_names() -> Vec<String> {
    let mut names = transact_names();
    names.extend(SLICE.iter().map(|(id, _)| format!("corpus/{id}")));
    names.extend(
        ["block/transfers", "block/storage_writes", "block/steady_pre_block"].map(String::from),
    );
    names.extend(["factory/create_evm", "factory/create_evm_with_inspector"].map(String::from));
    names
}

fn build_rows(want: &dyn Fn(&str) -> bool) -> Vec<Row> {
    let mut rows = transact_rows(want);
    rows.extend(corpus_rows(want));
    rows.extend(block_rows(want));
    rows.extend(factory_rows(want));
    rows
}

fn main() {
    if std::env::var_os("ARO_PROBE_LIST").is_some() {
        println!("ROWS {}", row_names().join(" "));
        return;
    }
    if let Some(only) = std::env::var("ARO_PROBE_ROW").ok().filter(|r| !r.is_empty()) {
        assert!(row_names().contains(&only), "no row {only}: see ARO_PROBE_LIST=1");
        let iters: u32 =
            std::env::var("ARO_PROBE_ITERS").ok().and_then(|s| s.parse().ok()).unwrap_or(8);
        let mut rows = build_rows(&|name| name == only);
        assert_eq!(rows.len(), 1, "row {only} built once");
        let row = &mut rows[0];
        for _ in 0..WARMUP_RUNS {
            black_box((row.run)());
        }
        for _ in 0..iters {
            black_box((row.run)());
        }
        println!("ROWRAN {only} {iters}");
        return;
    }

    let spin_secs: Option<u64> = std::env::args().nth(1).and_then(|s| s.parse().ok());
    let scale: u64 =
        std::env::var("ARO_BENCH_SCALE").ok().and_then(|s| s.parse().ok()).unwrap_or(1);
    let mut rows = build_rows(&|_| true);
    assert_eq!(
        rows.iter().map(|r| r.name.clone()).collect::<Vec<_>>(),
        row_names(),
        "every row built, in order"
    );
    let mut per_row = vec![Duration::ZERO; rows.len()];
    let round = |rows: &mut [Row], per_row: &mut [Duration]| -> Duration {
        let mut total = Duration::ZERO;
        for (row, spent) in rows.iter_mut().zip(per_row.iter_mut()) {
            for _ in 0..row.weight {
                let elapsed = (row.run)();
                *spent += elapsed;
                total += elapsed;
            }
        }
        total
    };

    if let Some(secs) = spin_secs {
        let deadline = Instant::now() + Duration::from_secs(secs);
        let mut n = 0u64;
        while Instant::now() < deadline {
            round(&mut rows, &mut per_row);
            n += 1;
        }
        println!("SPUN {n} rounds in {secs}s");
        return;
    }

    let reps = 50 * scale;
    for _ in 0..WARMUP_RUNS {
        round(&mut rows, &mut per_row);
    }
    per_row.iter_mut().for_each(|d| *d = Duration::ZERO);
    let mut samples = Vec::with_capacity(5);
    for _ in 0..5 {
        let mut total = Duration::ZERO;
        for _ in 0..reps {
            total += round(&mut rows, &mut per_row);
        }
        samples.push(total.as_nanos() as f64 / reps as f64);
    }
    let all: Duration = per_row.iter().sum();
    let runs = 5 * reps;
    let mut realistic = Duration::ZERO;
    for (row, spent) in rows.iter().zip(&per_row) {
        if row.name.starts_with("block/") || row.name.starts_with("corpus/") {
            realistic += *spent;
        }
        println!(
            "ROW {} ns={:.0} share={:.1}%",
            row.name,
            spent.as_nanos() as f64 / (runs * u64::from(row.weight)) as f64,
            100.0 * spent.as_secs_f64() / all.as_secs_f64()
        );
    }
    println!(
        "MIX realistic={:.1}% micro={:.1}%",
        100.0 * realistic.as_secs_f64() / all.as_secs_f64(),
        100.0 * (all - realistic).as_secs_f64() / all.as_secs_f64()
    );
    let line = samples.iter().map(|s| format!("{s:.0}")).collect::<Vec<_>>().join(" ");
    println!("BENCH {line}");
}
