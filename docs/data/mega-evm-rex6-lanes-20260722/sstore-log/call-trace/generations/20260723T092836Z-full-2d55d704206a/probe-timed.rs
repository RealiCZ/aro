//! ARO Lane 1 timed probe: REX6 SSTORE/SLOAD/LOG state transitions.
//! Workload v3 mirrors PR #330's `mega_contract_workload`: fresh DB/EVM,
//! funded caller, 10B gas, empty calldata/value/envelope, and production
//! `MegaEvm::transact(MegaTransaction)` for every fixed variant.
//! One logical workload is the ordered `scenarios()` list with each scenario's
//! fixed 100/50 internal opcode repetitions. Warmup, samples, scale, and spin
//! only repeat that complete logical workload as a timed measurement wrapper.

use std::hint::black_box;
use std::time::{Duration, Instant};

use alloy_primitives::{address, Address, Bytes, U256};
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

fn run_scenario(scenario: &Scenario) -> u64 {
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
    let result = evm.transact(mega_tx).expect("mega transact");
    assert!(result.result.is_success(), "fixed workload must succeed");
    black_box(scenario.name);
    result
        .result
        .gas_used()
        .wrapping_add(result.result.logs().len() as u64)
}

fn run_workload(scenarios: &[Scenario]) -> u64 {
    let mut acc = 0u64;
    for scenario in scenarios {
        acc = acc.wrapping_add(run_scenario(scenario));
    }
    acc
}

fn main() {
    black_box((WORKLOAD_ID, WORKLOAD_VERSION));
    let scenarios = scenarios();
    let spin_secs = std::env::args().nth(1).and_then(|s| s.parse::<u64>().ok());
    let scale = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(1);
    assert!(scale > 0, "ARO_BENCH_SCALE must be greater than zero");
    let mut acc = 0u64;

    if let Some(secs) = spin_secs {
        let deadline = Instant::now() + Duration::from_secs(secs.max(1));
        let mut runs = 0u64;
        while Instant::now() < deadline {
            acc = acc.wrapping_add(run_workload(&scenarios));
            runs += 1;
        }
        black_box(acc);
        println!("SPUN {} workloads in {}s", runs, secs);
        return;
    }

    acc = acc.wrapping_add(run_workload(&scenarios));
    let repetitions = scale;
    let mut samples = Vec::with_capacity(5);
    for _ in 0..5 {
        let start = Instant::now();
        for _ in 0..repetitions {
            acc = acc.wrapping_add(run_workload(&scenarios));
        }
        samples.push(start.elapsed().as_nanos() as f64 / repetitions as f64);
    }
    black_box(acc);
    let line = samples
        .iter()
        .map(|v| format!("{v:.0}"))
        .collect::<Vec<_>>()
        .join(" ");
    println!("BENCH {}", line);
}
