//! ARO Lane 3 differential oracle: REX6 SELFDESTRUCT beneficiary.
//! Fingerprint halt/success, gas, output, logs, sorted state (balances/nonces/code).

use std::hint::black_box;
use alloy_primitives::{address, keccak256, Address, Bytes, U256};
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

struct Scenario { name: &'static str, beneficiary: Address, fund_beneficiary: bool }

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

fn push_len(buf: &mut Vec<u8>, n: usize) { buf.extend_from_slice(&(n as u64).to_be_bytes()); }
fn push_bytes(buf: &mut Vec<u8>, bytes: &[u8]) { push_len(buf, bytes.len()); buf.extend_from_slice(bytes); }

fn encode_outcome(encoded: &mut Vec<u8>, sc: &Scenario) {
    push_bytes(encoded, sc.name.as_bytes());
    let mut db = CacheDB::<EmptyDB>::default();
    let code = selfdestruct_code(sc.beneficiary);
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    db.insert_account_info(CONTRACT, AccountInfo {
        balance: U256::from(10).pow(U256::from(18)), code: Some(bytecode), code_hash, nonce: 1, ..Default::default()
    });
    db.insert_account_info(CALLER, AccountInfo { balance: U256::from(10).pow(U256::from(18)), ..Default::default() });
    if sc.fund_beneficiary {
        db.insert_account_info(sc.beneficiary, AccountInfo { balance: U256::from(1), nonce: 1, ..Default::default() });
    }
    let mut context = MegaContext::new(db, SPEC);
    context.chain_mut().operator_fee_scalar = Some(U256::ZERO);
    context.chain_mut().operator_fee_constant = Some(U256::ZERO);
    let mut evm = MegaEvm::<_, NoOpInspector, EmptyExternalEnv>::new(context);
    let tx_env = TxEnvBuilder::new().caller(CALLER).call(CONTRACT).gas_limit(GAS_LIMIT).value(U256::ZERO).data(Bytes::new()).build_fill();
    let mut mega_tx = MegaTransaction::new(tx_env);
    mega_tx.enveloped_tx = Some(Bytes::new());
    let returned = match evm.transact(mega_tx) {
        Ok(r) => r,
        Err(_) => { encoded.push(3); return; }
    };
    let result = returned.result;
    let class = if result.is_success() { 0 } else if result.is_halt() { 2 } else { 1 };
    encoded.push(class);
    encoded.extend_from_slice(&result.gas_used().to_be_bytes());
    let output = result.output().cloned().unwrap_or_default();
    push_bytes(encoded, output.as_ref());
    let logs = result.logs();
    push_len(encoded, logs.len());
    for log in logs {
        encoded.extend_from_slice(log.address.as_slice());
        push_len(encoded, log.data.topics().len());
        for topic in log.data.topics() { encoded.extend_from_slice(topic.as_slice()); }
        push_bytes(encoded, log.data.data.as_ref());
    }
    let mut state_entries = returned.state.into_iter().collect::<Vec<_>>();
    state_entries.sort_unstable_by(|(a,_),(b,_)| a.as_slice().cmp(b.as_slice()));
    push_len(encoded, state_entries.len());
    for (address, account) in state_entries {
        encoded.extend_from_slice(address.as_slice());
        encoded.extend_from_slice(&account.info.balance.to_be_bytes::<32>());
        encoded.extend_from_slice(&account.info.nonce.to_be_bytes());
        encoded.extend_from_slice(account.info.code_hash.as_slice());
        encoded.extend_from_slice(&[account.is_touched() as u8, account.is_created() as u8, account.is_selfdestructed() as u8]);
        if let Some(code) = account.info.code.as_ref() {
            encoded.push(1); push_bytes(encoded, code.original_bytes().as_ref());
        } else { encoded.push(0); }
        let mut slots = account.storage.into_iter().collect::<Vec<_>>();
        slots.sort_unstable_by(|(a,_),(b,_)| a.cmp(b));
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
    for sc in scenarios() { encode_outcome(&mut encoded, &sc); }
    let digest = keccak256(encoded);
    black_box(digest);
    println!("DIFF {digest:x}");
}
