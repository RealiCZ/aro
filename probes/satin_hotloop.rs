//! ARO bench probe for the Satin engine (mega-evm, spec `SATIN`): one hot loop over a fixed mix
//! of Satin workloads, realistic and micro, built as the crate's `block`, `corpus` and `transact`
//! benches build theirs.
//!
//! Rows, each run once per round in this order (`ROW` lines name them):
//!
//! - `block/transfers`: a block of 64 value transfers through `MegaBlockExecutor`, on the steady
//!   state of `benches/block.rs` (predeploys as a chain that ran Rex6 holds them, the EIP-2935
//!   and EIP-4788 contracts, the pre-block changes, the spec's limits);
//! - `block/token_transfers`: a block of 64 ERC-20 `transfer`s on the same state, half to holders
//!   and half to new holders, so every transaction reads and writes two balance slots and logs;
//! - `corpus/<id>`: the 13 scenarios of `benches/corpus.rs`, each a block run on one `MegaEvm`
//!   with every transaction committed before the next;
//! - `transact/<workload>`: one transaction of a `benches/transact.rs` workload (its Satin arm),
//!   on one `MegaEvm` reused across runs, as a block executor reuses one.
//!
//! The realistic rows (`block`, `corpus`, each scenario run four times a round) carry about two
//! thirds of a round. A row's setup — a
//! fresh `State` over the block's database, a scenario's pre-state — is outside its timed section,
//! as in the criterion benches, and every row is run once and checked before anything is timed.
//!
//! Modes (the ARO probe contract):
//!   aro_satin_hotloop          bench: one `ROW` line per row (ns per run, share of the round),
//!                              then 5 samples of ns per round -> `BENCH s1 .. s5`; rounds per
//!                              sample scale with ARO_BENCH_SCALE
//!   aro_satin_hotloop <secs>   profile: spin rounds until the deadline -> `SPUN n rounds`

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
use alloy_primitives::{address, keccak256, Address, Bytes, Signature, TxKind, B256, U256};
use alloy_sol_types::SolCall;
use mega_evm::{
    system::{
        IMegaAccessControl, IMegaLimitControl, ACCESS_CONTROL_ADDRESS, ACCESS_CONTROL_CODE,
        LIMIT_CONTROL_ADDRESS, LIMIT_CONTROL_CODE, MEGA_SYSTEM_ADDRESS, ORACLE_CONTRACT_ADDRESS,
    },
    test_utils::{
        op_transaction, zero_fee_l1_block_info, BytecodeBuilder, CommittedStates, MemoryDatabase,
        Scenario,
    },
    BlockLimits, MegaBlockExecutionCtx, MegaBlockExecutor, MegaContext, MegaEvm, MegaEvmFactory,
    MegaHardforkConfig, MegaSpecId, MegaTxEnvelope,
};
use op_revm::OpTransaction;
use revm::{
    bytecode::opcode::{
        ADDRESS, BALANCE, CALL, CALLDATACOPY, CALLDATALOAD, CALLER, CODECOPY, DUP1, DUP3, DUP4,
        EQ, EXP, GAS, ISZERO, JUMPDEST, JUMPI, KECCAK256, LOG0, LOG2, LOG3, LT, MCOPY, MSTORE,
        NUMBER, POP, PUSH0, PUSH1, PUSH32, PUSH4, RETURN, RETURNDATACOPY, REVERT, SHR, SLOAD,
        SSTORE, STATICCALL, STOP, SUB, SWAP1, TIMESTAMP,
    },
    context::{BlockEnv, CfgEnv, TxEnv},
    database::State,
    DatabaseCommit, ExecuteEvm,
};

// ---- the block rows (benches/block.rs) -----------------------------------------------------

/// Transactions per block.
const N: u64 = 64;
const CHAIN_ID: u64 = 4_326;
const BLOCK_NUMBER: u64 = 1_000;
const BLOCK_TIMESTAMP: u64 = 1_800_000_000;
const PARENT_HASH: B256 = B256::repeat_byte(0xab);
const PARENT_BEACON_ROOT: B256 = B256::repeat_byte(0xcd);
const SENDER: Address = address!("0x2000000000000000000000000000000000000002");
const RECIPIENT: Address = address!("0x3000000000000000000000000000000000000003");
const TOKEN: Address = address!("0x4000000000000000000000000000000000000004");
/// The first of the `N` token holders the token block pays; the first half hold a balance.
const HOLDER_BASE: u64 = 0x5000_0000;

/// An ERC-20 `transfer(address,uint256)`: balances in a mapping at slot 0, a `Transfer` log, a
/// `true` answer; reverts on another selector or a short balance.
fn token_code() -> Bytes {
    let topic = keccak256("Transfer(address,address,uint256)");
    let mut c = vec![PUSH0, CALLDATALOAD, PUSH1, 0xe0, SHR, PUSH4, 0xa9, 0x05, 0x9c, 0xbb, EQ];
    let to_ok = c.len() + 1;
    c.extend([PUSH1, 0, JUMPI, PUSH0, PUSH0, REVERT]);
    c[to_ok] = c.len() as u8;
    // from = keccak(caller . 0); require balance >= amount; balance -= amount
    c.extend([JUMPDEST, CALLER, PUSH0, MSTORE, PUSH0, PUSH1, 0x20, MSTORE, PUSH1, 0x40, PUSH0]);
    c.extend([KECCAK256, DUP1, SLOAD, PUSH1, 0x24, CALLDATALOAD, DUP1, DUP3, LT]);
    let to_fail = c.len() + 1;
    c.extend([PUSH1, 0, JUMPI, DUP1, DUP3, SUB, DUP4, SSTORE]);
    // to = keccak(to . 0); balance += amount
    c.extend([PUSH1, 0x04, CALLDATALOAD, PUSH0, MSTORE, PUSH1, 0x40, PUSH0, KECCAK256]);
    c.extend([DUP1, SLOAD, DUP3, revm::bytecode::opcode::ADD, SWAP1, SSTORE]);
    // Transfer(caller, to, amount); return true
    c.extend([PUSH0, MSTORE, PUSH1, 0x04, CALLDATALOAD, CALLER, PUSH32]);
    c.extend_from_slice(topic.as_slice());
    c.extend([PUSH1, 0x20, PUSH0, LOG3, PUSH1, 1, PUSH0, MSTORE, PUSH1, 0x20, PUSH0, RETURN]);
    c[to_fail] = c.len() as u8;
    c.extend([JUMPDEST, PUSH0, PUSH0, REVERT]);
    assert!(c.len() < 256, "every jump target fits a PUSH1");
    Bytes::from(c)
}

fn balance_slot(holder: Address) -> U256 {
    let mut key = [0u8; 64];
    key[12..32].copy_from_slice(holder.as_slice());
    U256::from_be_bytes(keccak256(key).0)
}

fn holder(i: u64) -> Address {
    Address::from_word(B256::from(U256::from(HOLDER_BASE + i)))
}

fn transfer_input(to: Address, amount: u64) -> Bytes {
    let mut input = vec![0xa9, 0x05, 0x9c, 0xbb];
    input.extend_from_slice(B256::left_padding_from(to.as_slice()).as_slice());
    input.extend_from_slice(&U256::from(amount).to_be_bytes::<32>());
    Bytes::from(input)
}

fn block_database() -> MemoryDatabase {
    let mut db = MemoryDatabase::default().rex6_predeploys(MEGA_SYSTEM_ADDRESS);
    db.set_account_code(RECIPIENT, Bytes::new());
    db.set_account_balance(SENDER, U256::from(1_000_000_000_000_000_u64));
    db.set_account_code(HISTORY_STORAGE_ADDRESS, HISTORY_STORAGE_CODE.clone());
    db.set_account_code(BEACON_ROOTS_ADDRESS, BEACON_ROOTS_CODE.clone());
    db.set_account_code(TOKEN, token_code());
    db.set_account_storage(TOKEN, balance_slot(SENDER), U256::from(1_000_000_u64));
    for i in 0..N / 2 {
        db.set_account_storage(TOKEN, balance_slot(holder(i)), U256::from(1_000_u64));
    }
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

fn block_ctx() -> MegaBlockExecutionCtx {
    MegaBlockExecutionCtx::new(PARENT_HASH, Some(PARENT_BEACON_ROOT), Bytes::new(), BlockLimits::default())
}

fn chain_spec() -> MegaHardforkConfig {
    MegaHardforkConfig::default().with_all_activated()
}

/// The database a steady block starts from: the previous block's pre-block changes committed in
/// the order the executor commits them.
fn steady_database() -> MemoryDatabase {
    let mut db = block_database();
    let spec = chain_spec();
    let mut state = State::builder().with_database(db.clone()).build();
    let committed = CommittedStates::install(&mut state);
    let evm = MegaEvmFactory::new().create_evm(&mut state, evm_env_at(BLOCK_NUMBER - 1));
    let mut executor =
        MegaBlockExecutor::new(evm, block_ctx(), &spec, OpAlloyReceiptBuilder::default());
    executor.apply_pre_execution_changes().expect("the pre-block changes apply");
    drop(executor);
    for changes in committed.states() {
        db.commit(changes);
    }
    db
}

fn block_transactions(
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
            let signed = Signed::new_unchecked(tx, Signature::test_signature(), B256::repeat_byte(1));
            Recovered::new_unchecked(MegaTxEnvelope::Legacy(signed), SENDER)
        })
        .collect()
}

/// One block of `txs` on a fresh `State` over `steady`; returns the timed section and the
/// block's execution gas.
fn run_block(
    steady: &MemoryDatabase,
    spec: &MegaHardforkConfig,
    txs: &[Recovered<MegaTxEnvelope>],
    check: Option<Option<Address>>,
) -> (Duration, u64) {
    let mut state = State::builder().with_database(steady.clone()).build();
    let start = Instant::now();
    let gas = {
        let evm = MegaEvmFactory::new().create_evm(&mut state, evm_env_at(BLOCK_NUMBER));
        let mut executor =
            MegaBlockExecutor::new(evm, block_ctx(), spec, OpAlloyReceiptBuilder::default());
        executor.apply_pre_execution_changes().unwrap();
        for tx in txs {
            if let Some(logger) = check {
                let outcome = executor.execute_transaction_without_commit(tx).unwrap();
                assert!(outcome.result.is_success(), "{:?}", outcome.result);
                assert_eq!(outcome.limit_exceeded, None, "no limit stops the workload");
                assert!(outcome.usage.write_records > 0, "every transaction keeps a write record");
                if let Some(logger) = logger {
                    assert!(
                        outcome.result.logs().iter().any(|log| log.address == logger),
                        "every transaction logs from {logger}"
                    );
                }
                executor.commit_transaction(outcome);
            } else {
                black_box(executor.execute_transaction(tx).unwrap());
            }
        }
        let (_, result) = executor.finish_with_counters().unwrap();
        assert_eq!(result.receipts().len(), N as usize);
        black_box(result.gas.execution)
    };
    let elapsed = start.elapsed();
    drop(black_box(state));
    (elapsed, gas)
}

// ---- the transact rows (benches/transact.rs, Satin arm) ------------------------------------

const CALLER_EOA: Address = address!("0x0000000000000000000000000000000000100000");
const CALLEE: Address = address!("0x0000000000000000000000000000000000100001");
const RECURSIVE: Address = address!("0x0000000000000000000000000000000000100002");
const WRITER: Address = address!("0x0000000000000000000000000000000000100003");
const LOGGER: Address = address!("0x0000000000000000000000000000000000100004");
const INTERCEPTED: Address = address!("0x0000000000000000000000000000000000100007");
const READER: Address = address!("0x000000000000000000000000000000000010000a");
const VOLATILE: Address = address!("0x000000000000000000000000000000000010000b");
const HASHER: Address = address!("0x000000000000000000000000000000000010000c");
const VALUE_CALLER: Address = address!("0x000000000000000000000000000000000010000d");
const QUERIER: Address = address!("0x000000000000000000000000000000000010000e");
const DEPTH: u8 = 64;
const REPEAT: u64 = 200;
const DONE: u8 = 0x1f;

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
        code = code.push_number(topic).push_number(topic).push_number(32_u8).append(PUSH0).append(LOG2);
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
            .push_address(Address::ZERO)
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

fn value_caller_code() -> Bytes {
    let mut code = BytecodeBuilder::default();
    for _ in 0..REPEAT {
        code = code.call(CALLEE, U256::from(1)).append(POP);
    }
    code.stop().build()
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

fn transact_database() -> MemoryDatabase {
    MemoryDatabase::default()
        .account_balance(CALLER_EOA, U256::from(10u64.pow(18)))
        .account_balance(CALLEE, U256::from(1))
        .account_code(RECURSIVE, recursive_code())
        .account_code(WRITER, writer_code())
        .account_code(LOGGER, logger_code())
        .account_code(
            INTERCEPTED,
            system_caller_code(
                ACCESS_CONTROL_ADDRESS,
                IMegaAccessControl::isVolatileDataAccessDisabledCall::SELECTOR,
            ),
        )
        .account_code(
            QUERIER,
            system_caller_code(LIMIT_CONTROL_ADDRESS, IMegaLimitControl::remainingComputeGasCall::SELECTOR),
        )
        .account_balance(VALUE_CALLER, U256::from(REPEAT))
        .account_code(VALUE_CALLER, value_caller_code())
        .account_code(READER, reader_code())
        .account_code(VOLATILE, volatile_code())
        .account_code(HASHER, hasher_code())
        .account_code(ORACLE_CONTRACT_ADDRESS, reader_code())
        .account_code(ACCESS_CONTROL_ADDRESS, ACCESS_CONTROL_CODE)
        .account_code(LIMIT_CONTROL_ADDRESS, LIMIT_CONTROL_CODE)
}

fn call_tx(to: Address, data: Bytes, value: U256, gas_limit: u64) -> OpTransaction<TxEnv> {
    op_transaction(TxEnv {
        caller: CALLER_EOA,
        kind: TxKind::Call(to),
        data,
        value,
        gas_limit,
        ..Default::default()
    })
}

fn transact_workloads() -> Vec<(&'static str, OpTransaction<TxEnv>)> {
    let mut depth = [0u8; 32];
    depth[31] = DEPTH;
    let none = Bytes::new;
    vec![
        ("ether_transfer", call_tx(CALLEE, none(), U256::from(1), 1_000_000)),
        ("deep_calls", call_tx(RECURSIVE, Bytes::from(depth.to_vec()), U256::ZERO, 30_000_000)),
        ("storage_writes", call_tx(WRITER, none(), U256::ZERO, 30_000_000)),
        ("logs", call_tx(LOGGER, none(), U256::ZERO, 30_000_000)),
        ("calldata", call_tx(CALLEE, Bytes::from(vec![0xab_u8; 4096]), U256::ZERO, 30_000_000)),
        ("intercepted_calls", call_tx(INTERCEPTED, none(), U256::ZERO, 30_000_000)),
        ("remaining_compute_gas", call_tx(QUERIER, none(), U256::ZERO, 30_000_000)),
        ("value_calls", call_tx(VALUE_CALLER, none(), U256::ZERO, 30_000_000)),
        ("storage_reads", call_tx(READER, none(), U256::ZERO, 30_000_000)),
        ("volatile_reads", call_tx(VOLATILE, none(), U256::ZERO, 30_000_000)),
        ("hashes_and_copies", call_tx(HASHER, Bytes::from(vec![0xab_u8; 32]), U256::ZERO, 30_000_000)),
        ("oracle_reads", call_tx(ORACLE_CONTRACT_ADDRESS, none(), U256::ZERO, 30_000_000)),
    ]
}

// ---- the corpus rows (benches/corpus.rs) ---------------------------------------------------

const CORPUS: &[(&str, &str)] = &[
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

fn load_scenario(name: &str) -> Scenario {
    let file = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("benches/scenarios/{name}.json"));
    let text = std::fs::read_to_string(&file).unwrap_or_else(|err| panic!("{file:?}: {err}"));
    let scenario: Scenario = serde_json::from_str(&text).unwrap_or_else(|err| panic!("{file:?}: {err}"));
    scenario.validate().unwrap_or_else(|err| panic!("{file:?}: {err}"));
    scenario
}

// ---- the mix ---------------------------------------------------------------------------------

/// A row: its name, how many times a round runs it, and a run that returns its timed section
/// and a value to keep.
struct Row {
    name: String,
    weight: u32,
    run: Box<dyn FnMut() -> (Duration, u64)>,
}

/// Runs of each corpus scenario per round: the scenarios are small blocks, and four runs each
/// bring the realistic rows to about two thirds of a round.
const CORPUS_WEIGHT: u32 = 4;

fn rows() -> Vec<Row> {
    let mut rows = Vec::new();

    let spec = chain_spec();
    let steady = steady_database();
    let blocks = [
        ("transfers", None, block_transactions(RECIPIENT, 1, 300_000, |_| Bytes::new())),
        (
            "token_transfers",
            Some(TOKEN),
            block_transactions(TOKEN, 0, 500_000, |i| transfer_input(holder(i), 1)),
        ),
    ];
    for (name, logger, txs) in blocks {
        run_block(&steady, &spec, &txs, Some(logger));
        let (steady, spec) = (steady.clone(), chain_spec());
        rows.push(Row {
            name: format!("block/{name}"),
            weight: 1,
            run: Box::new(move || run_block(&steady, &spec, &txs, None)),
        });
    }

    for (id, file) in CORPUS {
        let scenario = load_scenario(file);
        let db = scenario.database();
        let outcomes = scenario.run_on(&mut scenario.evm(db.clone()));
        assert_eq!(outcomes.len(), scenario.txs.len(), "corpus/{id}: every transaction ran");
        rows.push(Row {
            name: format!("corpus/{id}"),
            weight: CORPUS_WEIGHT,
            run: Box::new(move || {
                let mut evm = scenario.evm(db.clone());
                let start = Instant::now();
                let outcomes = scenario.run_on(&mut evm);
                let elapsed = start.elapsed();
                let gas = outcomes
                    .iter()
                    .map(|o| o.as_ref().map_or(1, |o| o.result.tx_gas_used()))
                    .fold(0u64, u64::wrapping_add);
                drop(black_box(evm));
                (elapsed, black_box(gas))
            }),
        });
    }

    let db = transact_database();
    for (name, tx) in transact_workloads() {
        let mut evm = MegaEvm::new(MegaContext::new(db.clone(), MegaSpecId::SATIN).with_chain(zero_fee_l1_block_info()));
        let checked = evm.transact(OpTx(tx.clone())).unwrap();
        assert!(checked.result.is_success(), "transact/{name}: {:?}", checked.result);
        rows.push(Row {
            name: format!("transact/{name}"),
            weight: 1,
            run: Box::new(move || {
                let start = Instant::now();
                let out = evm.transact(OpTx(black_box(tx.clone()))).unwrap();
                let elapsed = start.elapsed();
                (elapsed, black_box(out.result.tx_gas_used()))
            }),
        });
    }
    rows
}

fn main() {
    let spin_secs: Option<u64> = std::env::args().nth(1).and_then(|s| s.parse().ok());
    let scale: u64 = std::env::var("ARO_BENCH_SCALE").ok().and_then(|s| s.parse().ok()).unwrap_or(1);
    let mut rows = rows();
    let mut acc = 0u64;
    let mut round = |rows: &mut [Row], per_row: &mut [Duration]| -> Duration {
        let mut total = Duration::ZERO;
        for (row, spent) in rows.iter_mut().zip(per_row.iter_mut()) {
            for _ in 0..row.weight {
                let (elapsed, value) = (row.run)();
                acc = acc.wrapping_add(value);
                *spent += elapsed;
                total += elapsed;
            }
        }
        total
    };
    let mut per_row = vec![Duration::ZERO; rows.len()];

    if let Some(secs) = spin_secs {
        let deadline = Instant::now() + Duration::from_secs(secs);
        let mut n = 0u64;
        while Instant::now() < deadline {
            round(&mut rows, &mut per_row);
            n += 1;
        }
        println!("SPUN {n} rounds in {secs}s");
    } else {
        let reps = 100 * scale;
        for _ in 0..2 {
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
            if !row.name.starts_with("transact/") {
                realistic += *spent;
            }
            println!(
                "ROW {} ns={:.0} share={:.1}%",
                row.name,
                spent.as_nanos() as f64 / (runs * u64::from(row.weight)) as f64,
                100.0 * spent.as_secs_f64() / all.as_secs_f64()
            );
        }
        println!("MIX realistic={:.1}% micro={:.1}%", 100.0 * realistic.as_secs_f64() / all.as_secs_f64(),
            100.0 * (all - realistic).as_secs_f64() / all.as_secs_f64());
        let line = samples.iter().map(|s| format!("{s:.0}")).collect::<Vec<_>>().join(" ");
        println!("BENCH {line}");
    }
    black_box(acc);
}
