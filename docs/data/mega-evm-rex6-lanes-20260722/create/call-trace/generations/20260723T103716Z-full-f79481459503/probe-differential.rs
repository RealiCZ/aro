//! ARO Lane 2 differential oracle for CREATE/CREATE2 workload v1.
//! Fingerprint: halt/success class, gas, output, ordered logs, address/slot-sorted
//! returned state (nonce/balance/code_hash/code/created flags). EmptyExternalEnv.

use std::hint::black_box;

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
    chunk.resize(32, 0);
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
    payload.push(0x94);
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

fn push_len(buf: &mut Vec<u8>, n: usize) {
    buf.extend_from_slice(&(n as u64).to_be_bytes());
}

fn push_bytes(buf: &mut Vec<u8>, bytes: &[u8]) {
    push_len(buf, bytes.len());
    buf.extend_from_slice(bytes);
}

fn encode_outcome(encoded: &mut Vec<u8>, scenario: &Scenario) {
    push_bytes(encoded, scenario.name.as_bytes());
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
        db.insert_account_info(addr, AccountInfo { balance: bal, ..Default::default() });
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
