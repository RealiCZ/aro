//! ARO Lane 2 timed probe: REX6 CREATE/CREATE2 single-window metering.
//! Derived from PR #330 create_deploy/rex6/{create_10,create2_10} semantics.
//! BytecodeBuilder u64 push_number == PUSH8; init code PUSH1 0 PUSH1 0 RETURN.
//! Fresh DB/EVM per scenario via production MegaEvm::transact(MegaTransaction).

use std::hint::black_box;
use std::time::{Duration, Instant};

use alloy_primitives::{address, keccak256, Address, Bytes, U256};
use mega_evm::{
    revm::inspector::NoOpInspector, EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId,
    MegaTransaction,
};
use revm::{
    bytecode::opcode::{CREATE, CREATE2, MSTORE, POP, PUSH0, PUSH8},
    context::{tx::TxEnvBuilder, ContextTr},
    database::{CacheDB, EmptyDB},
    state::{AccountInfo, Bytecode},
    ExecuteEvm as _,
};

const WORKLOAD_ID: &str = "mega-evm-rex6-create";
const WORKLOAD_VERSION: &str = "1";
const SPEC: MegaSpecId = MegaSpecId::REX6;
const CALLER: Address = address!("0000000000000000000000000000000000100000");
const CONTRACT: Address = address!("0000000000000000000000000000000000100002");
const GAS_LIMIT: u64 = 10_000_000_000;
const N_DEPLOYS: usize = 10;
const INIT_CODE: [u8; 5] = [0x60, 0x00, 0x60, 0x00, 0xf3];

struct Scenario {
    name: &'static str,
    code: Bytes,
    prefund: Vec<(Address, U256)>,
}

fn push_u64(code: &mut Vec<u8>, value: u64) {
    code.push(PUSH8);
    code.extend_from_slice(&value.to_be_bytes());
}

fn push_bytes_raw(code: &mut Vec<u8>, bytes: &[u8]) {
    assert!(bytes.len() <= 32);
    code.push(PUSH0 + bytes.len() as u8);
    code.extend_from_slice(bytes);
}

fn mstore_right_pad(code: &mut Vec<u8>, offset: u64, bytes: &[u8]) {
    let mut chunk = bytes.to_vec();
    chunk.resize(32, 0); // right-pad (data left-aligned)
    push_bytes_raw(code, &chunk);
    push_u64(code, offset);
    code.push(MSTORE);
}

fn make_create_bytecode(n: usize) -> Bytes {
    let mut code = Vec::new();
    mstore_right_pad(&mut code, 0, &INIT_CODE);
    for _ in 0..n {
        push_u64(&mut code, 5);
        push_u64(&mut code, 0);
        push_u64(&mut code, 0);
        code.push(CREATE);
        code.push(POP);
    }
    code.into()
}

fn make_create2_bytecode(n: usize) -> Bytes {
    let mut code = Vec::new();
    mstore_right_pad(&mut code, 0, &INIT_CODE);
    for i in 0..n {
        push_u64(&mut code, i as u64);
        push_u64(&mut code, 5);
        push_u64(&mut code, 0);
        push_u64(&mut code, 0);
        code.push(CREATE2);
        code.push(POP);
    }
    code.into()
}

fn create_address(deployer: Address, nonce: u64) -> Address {
    let mut payload = Vec::new();
    payload.push(0x94); // 0x80+20
    payload.extend_from_slice(deployer.as_slice());
    if nonce == 0 {
        payload.push(0x80);
    } else if nonce < 0x80 {
        payload.push(nonce as u8);
    } else {
        let b = nonce.to_be_bytes();
        let start = b.iter().position(|&x| x != 0).unwrap_or(7);
        let nb = &b[start..];
        payload.push(0x80 + nb.len() as u8);
        payload.extend_from_slice(nb);
    }
    let mut rlp = Vec::new();
    rlp.push(0xc0 + payload.len() as u8);
    rlp.extend_from_slice(&payload);
    let h = keccak256(&rlp);
    Address::from_slice(&h[12..])
}

fn create2_address(deployer: Address, salt: U256, init_code: &[u8]) -> Address {
    let init_hash = keccak256(init_code);
    let mut buf = Vec::with_capacity(85);
    buf.push(0xff);
    buf.extend_from_slice(deployer.as_slice());
    buf.extend_from_slice(&salt.to_be_bytes::<32>());
    buf.extend_from_slice(init_hash.as_slice());
    let h = keccak256(&buf);
    Address::from_slice(&h[12..])
}

fn scenarios() -> Vec<Scenario> {
    let create_code = make_create_bytecode(N_DEPLOYS);
    let create2_code = make_create2_bytecode(N_DEPLOYS);
    // CONTRACT starts with nonce 1 as a contract account; first CREATE consumes nonce 1.
    let first_create = create_address(CONTRACT, 1);
    let first_create2 = create2_address(CONTRACT, U256::from(0u64), &INIT_CODE);
    vec![
        Scenario { name: "create_10_net_new", code: create_code.clone(), prefund: vec![] },
        Scenario { name: "create2_10_net_new", code: create2_code.clone(), prefund: vec![] },
        Scenario {
            name: "create_10_prefunded_no_code",
            code: create_code,
            prefund: vec![(first_create, U256::from(10u64).pow(U256::from(18)))],
        },
        Scenario {
            name: "create2_10_prefunded_no_code",
            code: create2_code,
            prefund: vec![(first_create2, U256::from(10u64).pow(U256::from(18)))],
        },
    ]
}

fn run_scenario(scenario: &Scenario) -> u64 {
    // COMMON_SETUP_BEGIN
    let mut db = CacheDB::<EmptyDB>::default();
    let bytecode = Bytecode::new_legacy(scenario.code.clone());
    let code_hash = bytecode.hash_slow();
    db.insert_account_info(
        CONTRACT,
        AccountInfo { code: Some(bytecode), code_hash, nonce: 1, ..Default::default() },
    );
    db.insert_account_info(
        CALLER,
        AccountInfo {
            balance: U256::from(10).pow(U256::from(18)),
            ..Default::default()
        },
    );
    for &(addr, bal) in &scenario.prefund {
        db.insert_account_info(
            addr,
            AccountInfo { balance: bal, ..Default::default() },
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
    // COMMON_SETUP_END
    let result = evm.transact(mega_tx).expect("mega transact");
    assert!(result.result.is_success(), "fixed workload must succeed: {}", scenario.name);
    black_box(scenario.name);
    result.result.gas_used()
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
    println!("BENCH {line}");
}
