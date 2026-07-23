//! ARO Lane 3 timed probe: REX6 SELFDESTRUCT beneficiary accounting.
//! Derived from bench_selfdestruct semantics (PUSH0/SELFDESTRUCT generalized to
//! explicit beneficiary). Production MegaEvm::transact; fresh DB/EVM per case.

use std::hint::black_box;
use std::time::{Duration, Instant};

use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{
    revm::inspector::NoOpInspector, EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId,
    MegaTransaction,
};
use revm::{
    bytecode::opcode::{PUSH20, SELFDESTRUCT},
    context::{tx::TxEnvBuilder, ContextTr},
    database::{CacheDB, EmptyDB},
    state::{AccountInfo, Bytecode},
    ExecuteEvm as _,
};

const WORKLOAD_ID: &str = "mega-evm-rex6-selfdestruct";
const WORKLOAD_VERSION: &str = "1";
const SPEC: MegaSpecId = MegaSpecId::REX6;
const CALLER: Address = address!("0000000000000000000000000000000000100000");
const CONTRACT: Address = address!("0000000000000000000000000000000000100002");
const BENEF_EMPTY: Address = address!("0000000000000000000000000000000000b00001");
const BENEF_EXIST: Address = address!("0000000000000000000000000000000000b00002");
const GAS_LIMIT: u64 = 10_000_000_000;

struct Scenario {
    name: &'static str,
    beneficiary: Address,
    fund_beneficiary: bool,
}

fn selfdestruct_code(beneficiary: Address) -> Bytes {
    let mut code = Vec::new();
    code.push(PUSH20);
    code.extend_from_slice(beneficiary.as_slice());
    code.push(SELFDESTRUCT);
    code.into()
}

fn scenarios() -> Vec<Scenario> {
    vec![
        Scenario { name: "funded_to_empty", beneficiary: BENEF_EMPTY, fund_beneficiary: false },
        Scenario { name: "funded_to_existing", beneficiary: BENEF_EXIST, fund_beneficiary: true },
    ]
}

fn run_scenario(sc: &Scenario) -> u64 {
    let mut db = CacheDB::<EmptyDB>::default();
    let code = selfdestruct_code(sc.beneficiary);
    let bytecode = Bytecode::new_legacy(code.clone());
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
    db.insert_account_info(
        CALLER,
        AccountInfo { balance: U256::from(10).pow(U256::from(18)), ..Default::default() },
    );
    if sc.fund_beneficiary {
        db.insert_account_info(
            sc.beneficiary,
            AccountInfo { balance: U256::from(1), nonce: 1, ..Default::default() },
        );
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
    let result = evm.transact(mega_tx).expect("transact");
    // SELFDESTRUCT success under EIP-6780 still succeeds
    assert!(result.result.is_success() || result.result.is_halt(), "unexpected: {}", sc.name);
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
    let scale = std::env::var("ARO_BENCH_SCALE").ok().and_then(|s| s.parse::<u64>().ok()).unwrap_or(1);
    assert!(scale > 0, "ARO_BENCH_SCALE must be greater than zero");
    let mut acc = 0u64;
    if let Some(secs) = spin_secs {
        let deadline = Instant::now() + Duration::from_secs(secs.max(1));
        let mut runs = 0u64;
        while Instant::now() < deadline { acc = acc.wrapping_add(run_workload(&scenarios)); runs += 1; }
        black_box(acc); println!("SPUN {} workloads in {}s", runs, secs); return;
    }
    acc = acc.wrapping_add(run_workload(&scenarios));
    let mut samples = Vec::with_capacity(5);
    for _ in 0..5 {
        let start = Instant::now();
        for _ in 0..scale { acc = acc.wrapping_add(run_workload(&scenarios)); }
        samples.push(start.elapsed().as_nanos() as f64 / scale as f64);
    }
    black_box(acc);
    println!("BENCH {}", samples.iter().map(|v| format!("{v:.0}")).collect::<Vec<_>>().join(" "));
}
