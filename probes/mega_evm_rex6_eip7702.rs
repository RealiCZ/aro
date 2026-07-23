//! ARO Lane 4 timed probe: REX6 EIP-7702 authority accounting.
//! Derived from benches/tests eip7702_authlist + rex6 authority accounting.
//! Production MegaEvm::transact with recovered authorization lists.

use std::hint::black_box;
use std::time::{Duration, Instant};

use alloy_eips::eip7702::{Authorization, RecoveredAuthority, RecoveredAuthorization};
use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{
    revm::inspector::NoOpInspector, EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId,
    MegaTransaction,
};
use revm::{
    context::{tx::TxEnvBuilder, ContextTr},
    database::{CacheDB, EmptyDB},
    state::AccountInfo,
    ExecuteEvm as _,
};

const WORKLOAD_ID: &str = "mega-evm-rex6-eip7702";
const WORKLOAD_VERSION: &str = "1";
const SPEC: MegaSpecId = MegaSpecId::REX6;
const CALLER: Address = address!("0000000000000000000000000000000000800000");
const CALLEE: Address = address!("0000000000000000000000000000000000800001");
const DELEGATE: Address = address!("0000000000000000000000000000000000900001");
const GAS_LIMIT: u64 = 100_000_000;

struct Scenario {
    name: &'static str,
    n_auth: usize,
    chain_id: u64,
    authority_nonce: u64,
    /// If true, pre-fund authorities with matching nonce so application may differ.
    preexist: bool,
}

fn authority_i(i: usize) -> Address {
    let mut bytes = [0u8; 20];
    bytes[12..20].copy_from_slice(&((i as u64) + 0x0010_0000).to_be_bytes());
    Address::from(bytes)
}

fn make_auths(n: usize, chain_id: u64, nonce: u64) -> Vec<RecoveredAuthorization> {
    (0..n)
        .map(|i| {
            RecoveredAuthorization::new_unchecked(
                Authorization {
                    chain_id: U256::from(chain_id),
                    address: DELEGATE,
                    nonce,
                },
                RecoveredAuthority::Valid(authority_i(i)),
            )
        })
        .collect()
}

fn scenarios() -> Vec<Scenario> {
    vec![
        Scenario { name: "applied_net_new_32", n_auth: 32, chain_id: 0, authority_nonce: 0, preexist: false },
        Scenario { name: "applied_net_new_48", n_auth: 48, chain_id: 0, authority_nonce: 0, preexist: false },
        Scenario { name: "chain_id_mismatch_32", n_auth: 32, chain_id: 999, authority_nonce: 0, preexist: false },
        Scenario { name: "nonce_mismatch_32", n_auth: 32, chain_id: 0, authority_nonce: 5, preexist: false },
        Scenario { name: "applied_existing_nonce0_16", n_auth: 16, chain_id: 0, authority_nonce: 0, preexist: true },
    ]
}

fn run_scenario(sc: &Scenario) -> u64 {
    let mut db = CacheDB::<EmptyDB>::default();
    db.insert_account_info(
        CALLER,
        AccountInfo {
            balance: U256::from(10u64).pow(U256::from(24)),
            ..Default::default()
        },
    );
    db.insert_account_info(CALLEE, AccountInfo { balance: U256::from(1), ..Default::default() });
    if sc.preexist {
        for i in 0..sc.n_auth {
            db.insert_account_info(
                authority_i(i),
                AccountInfo { nonce: 0, balance: U256::from(1), ..Default::default() },
            );
        }
    }
    let mut context = MegaContext::new(db, SPEC);
    context.chain_mut().operator_fee_scalar = Some(U256::ZERO);
    context.chain_mut().operator_fee_constant = Some(U256::ZERO);
    let mut evm = MegaEvm::<_, NoOpInspector, EmptyExternalEnv>::new(context);
    let auths = make_auths(sc.n_auth, sc.chain_id, sc.authority_nonce);
    let tx_env = TxEnvBuilder::new()
        .caller(CALLER)
        .call(CALLEE)
        .gas_limit(GAS_LIMIT)
        .authorization_list_recovered(auths)
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
