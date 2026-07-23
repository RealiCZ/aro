//! ARO Lane 1 deterministic oracle for the timed REX6 logical workload v3.
//! Canonical output includes result class, gas, output, ordered logs, and
//! address/slot-sorted returned state. SALT bucket IDs are omitted because this
//! aligned pair uses `EmptyExternalEnv`; no bucket IDs are externally observable.
//! Internal journal/cache ordering, warm/cold markers, and intermediate tracker
//! state are likewise omitted because they are not returned execution/state fields.
//! This executes exactly one logical workload: the ordered `scenarios()` list with
//! each scenario's fixed 100/50 internal opcode repetitions. Timed-only outer
//! repetitions are a measurement wrapper and are not differential semantics.

use std::hint::black_box;

use alloy_primitives::{address, keccak256, Address, Bytes, U256};
use mega_evm::{
    revm::inspector::NoOpInspector, EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId,
    MegaTransaction,
};
use revm::{
    bytecode::opcode::{LOG0, LOG1, LOG2, POP, PUSH8, SLOAD, SSTORE},
    context::{tx::TxEnvBuilder, ContextTr},
    database::{CacheDB, EmptyDB},
    state::{AccountInfo, Bytecode},
    ExecuteEvm as _,
};

const WORKLOAD_ID: &str = "mega-evm-rex6-sstore-log";
const WORKLOAD_VERSION: &str = "3";
const SPEC: MegaSpecId = MegaSpecId::REX6;
const CALLER: Address = address!("0000000000000000000000000000000000100000");
const CONTRACT: Address = address!("0000000000000000000000000000000000100002");
const GAS_LIMIT: u64 = 10_000_000_000;
const STORAGE_ITERATIONS: usize = 100;
const LOG_ITERATIONS: usize = 50;

struct Scenario {
    name: &'static str,
    code: Bytes,
    storage: Vec<(U256, U256)>,
}

fn push_number(code: &mut Vec<u8>, value: u64) {
    code.push(PUSH8);
    code.extend_from_slice(&value.to_be_bytes());
}

fn storage_scenario(name: &'static str) -> Scenario {
    let mut code = Vec::new();
    let mut storage = Vec::new();
    for i in 0..STORAGE_ITERATIONS {
        match name {
            "zero_to_nonzero" => {
                push_number(&mut code, (i + 1) as u64);
                push_number(&mut code, i as u64);
                code.push(SSTORE);
            }
            "nonzero_to_nonzero" => {
                storage.push((U256::from(i), U256::from(i + 1)));
                push_number(&mut code, (i + 101) as u64);
                push_number(&mut code, i as u64);
                code.push(SSTORE);
            }
            "reset_to_original" => {
                storage.push((U256::from(i), U256::from(i + 1)));
                push_number(&mut code, (i + 101) as u64);
                push_number(&mut code, i as u64);
                code.push(SSTORE);
                push_number(&mut code, (i + 1) as u64);
                push_number(&mut code, i as u64);
                code.push(SSTORE);
            }
            "sload" => {
                push_number(&mut code, i as u64);
                code.extend_from_slice(&[SLOAD, POP]);
            }
            "sstore_sload" => {
                push_number(&mut code, (i + 1) as u64);
                push_number(&mut code, i as u64);
                code.push(SSTORE);
                push_number(&mut code, i as u64);
                code.extend_from_slice(&[SLOAD, POP]);
            }
            _ => unreachable!(),
        }
    }
    Scenario {
        name,
        code: code.into(),
        storage,
    }
}

fn log_scenario(name: &'static str, topics: usize, data_len: usize) -> Scenario {
    let mut code = Vec::new();
    for _ in 0..LOG_ITERATIONS {
        for topic in 0..topics {
            push_number(&mut code, (0x11 * (topic + 1)) as u64);
        }
        push_number(&mut code, data_len as u64);
        push_number(&mut code, 0);
        code.push(match topics {
            0 => LOG0,
            1 => LOG1,
            2 => LOG2,
            _ => unreachable!(),
        });
    }
    Scenario {
        name,
        code: code.into(),
        storage: Vec::new(),
    }
}

fn scenarios() -> Vec<Scenario> {
    vec![
        storage_scenario("zero_to_nonzero"),
        storage_scenario("nonzero_to_nonzero"),
        storage_scenario("reset_to_original"),
        storage_scenario("sload"),
        storage_scenario("sstore_sload"),
        log_scenario("log0_32", 0, 32),
        log_scenario("log1_64", 1, 64),
        log_scenario("log2_256", 2, 256),
    ]
}

fn push_len(encoded: &mut Vec<u8>, len: usize) {
    encoded.extend_from_slice(&(len as u64).to_be_bytes());
}

fn push_bytes(encoded: &mut Vec<u8>, bytes: &[u8]) {
    push_len(encoded, bytes.len());
    encoded.extend_from_slice(bytes);
}

fn encode_outcome(encoded: &mut Vec<u8>, scenario: &Scenario) {
    push_bytes(encoded, scenario.name.as_bytes());

    // COMMON_SETUP_BEGIN
    let mut db = CacheDB::<EmptyDB>::default();
    let bytecode = Bytecode::new_legacy(scenario.code.clone());
    let code_hash = bytecode.hash_slow();
    db.insert_account_info(
        CONTRACT,
        AccountInfo {
            code: Some(bytecode),
            code_hash,
            ..Default::default()
        },
    );
    db.insert_account_info(
        CALLER,
        AccountInfo {
            balance: U256::from(10).pow(U256::from(18)),
            ..Default::default()
        },
    );
    for &(slot, value) in &scenario.storage {
        db.insert_account_storage(CONTRACT, slot, value)
            .expect("seed storage");
    }

    let mut context = MegaContext::new(db, SPEC);
    context.chain_mut().operator_fee_scalar = Some(U256::ZERO);
    context.chain_mut().operator_fee_constant = Some(U256::ZERO);
    let mut evm = MegaEvm::<_, NoOpInspector, EmptyExternalEnv>::new(context);
    let tx_env = TxEnvBuilder::new()
        .caller(CALLER)
        .call(CONTRACT)
        .gas_limit(GAS_LIMIT)
        .value(U256::ZERO)
        .data(Bytes::new())
        .build_fill();
    let mut mega_tx = MegaTransaction::new(tx_env);
    mega_tx.enveloped_tx = Some(Bytes::new());
    // COMMON_SETUP_END
    let returned = match evm.transact(mega_tx) {
        Ok(returned) => returned,
        Err(_) => {
            encoded.push(3);
            return;
        }
    };
    let result = returned.result;
    let class = if result.is_success() {
        0
    } else if result.is_halt() {
        2
    } else {
        1
    };
    encoded.push(class);
    encoded.extend_from_slice(&result.gas_used().to_be_bytes());
    let output = result.output().cloned().unwrap_or_default();
    push_bytes(encoded, output.as_ref());

    let logs = result.logs();
    push_len(encoded, logs.len());
    for log in logs {
        encoded.extend_from_slice(log.address.as_slice());
        push_len(encoded, log.data.topics().len());
        for topic in log.data.topics() {
            encoded.extend_from_slice(topic.as_slice());
        }
        push_bytes(encoded, log.data.data.as_ref());
    }

    let mut state_entries = returned.state.into_iter().collect::<Vec<_>>();
    state_entries.sort_unstable_by(|(a, _), (b, _)| a.as_slice().cmp(b.as_slice()));
    push_len(encoded, state_entries.len());
    for (address, account) in state_entries {
        encoded.extend_from_slice(address.as_slice());
        encoded.extend_from_slice(&account.info.balance.to_be_bytes::<32>());
        encoded.extend_from_slice(&account.info.nonce.to_be_bytes());
        encoded.extend_from_slice(account.info.code_hash.as_slice());
        encoded.extend_from_slice(&[
            account.is_touched() as u8,
            account.is_created() as u8,
            account.is_selfdestructed() as u8,
        ]);
        if let Some(code) = account.info.code.as_ref() {
            let bytes = code.original_bytes();
            encoded.push(1);
            push_bytes(encoded, bytes.as_ref());
        } else {
            encoded.push(0);
        }
        let mut slots = account.storage.into_iter().collect::<Vec<_>>();
        slots.sort_unstable_by(|(a, _), (b, _)| a.cmp(b));
        push_len(encoded, slots.len());
        for (slot, value) in slots {
            encoded.extend_from_slice(&slot.to_be_bytes::<32>());
            encoded.extend_from_slice(&value.present_value.to_be_bytes::<32>());
        }
    }
}

fn main() {
    let mut encoded = Vec::new();
    push_bytes(&mut encoded, WORKLOAD_ID.as_bytes());
    push_bytes(&mut encoded, WORKLOAD_VERSION.as_bytes());
    for scenario in scenarios() {
        encode_outcome(&mut encoded, &scenario);
    }
    let digest = keccak256(encoded);
    black_box(digest);
    println!("DIFF {digest:x}");
}
