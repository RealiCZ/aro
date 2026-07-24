//! ARO Lane 5 timed probe: REX6 system-origin exemption / unscaled SALT.
//! Ordinary vs EIP-system caller on SSTORE and value-CALL-to-empty under min vs crowded SALT.

use std::convert::Infallible;
use std::hint::black_box;
use std::time::{Duration, Instant};

use alloy_eips::eip4788::SYSTEM_ADDRESS as EIP_SYSTEM_ADDRESS;
use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{
    revm::inspector::NoOpInspector, BucketHasher, MegaContext, MegaEvm, MegaSpecId,
    MegaTransaction, SimpleBucketHasher, TestExternalEnvs, MIN_BUCKET_SIZE,
};
use revm::{
    bytecode::opcode::{CALL, GAS, POP, PUSH20, PUSH8, SSTORE},
    context::{tx::TxEnvBuilder, ContextTr},
    database::{CacheDB, EmptyDB},
    state::{AccountInfo, Bytecode},
    ExecuteEvm as _,
};

const WORKLOAD_ID: &str = "mega-evm-rex6-system-salt";
const WORKLOAD_VERSION: &str = "1";
const SPEC: MegaSpecId = MegaSpecId::REX6;
const ORDINARY: Address = address!("0000000000000000000000000000000000100000");
const CONTRACT: Address = address!("0000000000000000000000000000000000100002");
const EMPTY_TARGET: Address = address!("0000000000000000000000000000000000e00001");
const GAS_LIMIT: u64 = 10_000_000_000;
const HEAVY_MULT: u64 = 100;

type Envs = TestExternalEnvs<Infallible, SimpleBucketHasher>;

#[derive(Clone, Copy)]
enum Kind { Sstore, CallEmpty }

struct Scenario {
    name: &'static str,
    system: bool,
    crowded: bool,
    kind: Kind,
}

fn push_u64(code: &mut Vec<u8>, v: u64) {
    code.push(PUSH8);
    code.extend_from_slice(&v.to_be_bytes());
}

fn sstore_code() -> Bytes {
    let mut code = Vec::new();
    push_u64(&mut code, 1);
    push_u64(&mut code, 0);
    code.push(SSTORE);
    code.into()
}

fn call_empty_code() -> Bytes {
    // CALL(gas, EMPTY_TARGET, value=1, 0,0,0,0); POP
    let mut code = Vec::new();
    push_u64(&mut code, 0); // retSize
    push_u64(&mut code, 0);
    push_u64(&mut code, 0);
    push_u64(&mut code, 0);
    push_u64(&mut code, 1); // value
    code.push(PUSH20);
    code.extend_from_slice(EMPTY_TARGET.as_slice());
    code.push(GAS);
    code.push(CALL);
    code.push(POP);
    code.into()
}

fn scenarios() -> Vec<Scenario> {
    vec![
        Scenario { name: "ordinary_sstore_min", system: false, crowded: false, kind: Kind::Sstore },
        Scenario { name: "ordinary_sstore_crowded", system: false, crowded: true, kind: Kind::Sstore },
        Scenario { name: "system_sstore_min", system: true, crowded: false, kind: Kind::Sstore },
        Scenario { name: "system_sstore_crowded", system: true, crowded: true, kind: Kind::Sstore },
        Scenario { name: "ordinary_call_empty_min", system: false, crowded: false, kind: Kind::CallEmpty },
        Scenario { name: "ordinary_call_empty_crowded", system: false, crowded: true, kind: Kind::CallEmpty },
        Scenario { name: "system_call_empty_min", system: true, crowded: false, kind: Kind::CallEmpty },
        Scenario { name: "system_call_empty_crowded", system: true, crowded: true, kind: Kind::CallEmpty },
    ]
}

fn envs_for(crowded: bool, addr: Address) -> Envs {
    if !crowded {
        return TestExternalEnvs::new();
    }
    let bucket = SimpleBucketHasher::bucket_id(addr.as_slice());
    TestExternalEnvs::new().with_bucket_capacity(bucket, MIN_BUCKET_SIZE as u64 * HEAVY_MULT)
}

fn run_scenario(sc: &Scenario) -> u64 {
    let code = match sc.kind {
        Kind::Sstore => sstore_code(),
        Kind::CallEmpty => call_empty_code(),
    };
    let mut db = CacheDB::<EmptyDB>::default();
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    db.insert_account_info(
        CONTRACT,
        AccountInfo {
            balance: U256::from(10).pow(U256::from(18)),
            code: Some(bytecode),
            code_hash,
            nonce: 1,
            ..Default::default()
        },
    );
    let caller = if sc.system { EIP_SYSTEM_ADDRESS } else { ORDINARY };
    db.insert_account_info(
        caller,
        AccountInfo { balance: U256::from(10).pow(U256::from(18)), ..Default::default() },
    );
    let salt_addr = match sc.kind {
        Kind::Sstore => CONTRACT,
        Kind::CallEmpty => EMPTY_TARGET,
    };
    let envs = envs_for(sc.crowded, salt_addr);
    let mut context = MegaContext::new(db, SPEC).with_external_envs(envs.into());
    context.chain_mut().operator_fee_scalar = Some(U256::ZERO);
    context.chain_mut().operator_fee_constant = Some(U256::ZERO);
    let mut evm = MegaEvm::<_, NoOpInspector, _>::new(context);
    let tx_env = TxEnvBuilder::new()
        .caller(caller)
        .call(CONTRACT)
        .gas_limit(GAS_LIMIT)
        .value(U256::ZERO)
        .data(Bytes::new())
        .build_fill();
    let mut mega_tx = MegaTransaction::new(tx_env);
    mega_tx.enveloped_tx = Some(Bytes::new());
    let result = evm.transact(mega_tx).expect("transact");
    black_box(sc.name);
    result.result.gas_used()
}

fn run_workload(scenarios: &[Scenario]) -> u64 {
    scenarios.iter().map(run_scenario).fold(0u64, u64::wrapping_add)
}

fn main() {
    black_box((WORKLOAD_ID, WORKLOAD_VERSION));
    let scenarios = scenarios();
    let spin_secs = std::env::args().nth(1).and_then(|s| s.parse::<u64>().ok());
    let scale = std::env::var("ARO_BENCH_SCALE").ok().and_then(|s| s.parse().ok()).unwrap_or(1u64);
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
        println!("SPUN {runs} workloads in {secs}s");
        return;
    }
    acc = acc.wrapping_add(run_workload(&scenarios));
    let mut samples = Vec::with_capacity(5);
    for _ in 0..5 {
        let start = Instant::now();
        for _ in 0..scale {
            acc = acc.wrapping_add(run_workload(&scenarios));
        }
        samples.push(start.elapsed().as_nanos() as f64 / scale as f64);
    }
    black_box(acc);
    println!(
        "BENCH {}",
        samples.iter().map(|v| format!("{v:.0}")).collect::<Vec<_>>().join(" ")
    );
}
