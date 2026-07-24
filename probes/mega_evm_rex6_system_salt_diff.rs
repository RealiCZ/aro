//! ARO Lane 5 differential: system-origin exemption / unscaled SALT sensitivity control.

use std::convert::Infallible;
use std::hint::black_box;

use alloy_eips::eip4788::SYSTEM_ADDRESS as EIP_SYSTEM_ADDRESS;
use alloy_primitives::{address, keccak256, Address, Bytes, U256};
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
struct Scenario { name: &'static str, system: bool, crowded: bool, kind: Kind }

fn push_u64(code: &mut Vec<u8>, v: u64) { code.push(PUSH8); code.extend_from_slice(&v.to_be_bytes()); }
fn sstore_code() -> Bytes {
    let mut code = Vec::new(); push_u64(&mut code, 1); push_u64(&mut code, 0); code.push(SSTORE); code.into()
}
fn call_empty_code() -> Bytes {
    let mut code = Vec::new();
    for _ in 0..4 { push_u64(&mut code, 0); }
    push_u64(&mut code, 1);
    code.push(PUSH20); code.extend_from_slice(EMPTY_TARGET.as_slice());
    code.push(GAS); code.push(CALL); code.push(POP); code.into()
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
    if !crowded { return TestExternalEnvs::new(); }
    let bucket = SimpleBucketHasher::bucket_id(addr.as_slice());
    TestExternalEnvs::new().with_bucket_capacity(bucket, MIN_BUCKET_SIZE as u64 * HEAVY_MULT)
}
fn push_len(buf: &mut Vec<u8>, n: usize) { buf.extend_from_slice(&(n as u64).to_be_bytes()); }
fn push_bytes(buf: &mut Vec<u8>, bytes: &[u8]) { push_len(buf, bytes.len()); buf.extend_from_slice(bytes); }

fn encode_outcome(encoded: &mut Vec<u8>, sc: &Scenario) {
    push_bytes(encoded, sc.name.as_bytes());
    encoded.push(sc.system as u8);
    encoded.push(sc.crowded as u8);
    let code = match sc.kind { Kind::Sstore => sstore_code(), Kind::CallEmpty => call_empty_code() };
    let mut db = CacheDB::<EmptyDB>::default();
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    db.insert_account_info(CONTRACT, AccountInfo {
        balance: U256::from(10).pow(U256::from(18)), code: Some(bytecode), code_hash, nonce: 1, ..Default::default()
    });
    let caller = if sc.system { EIP_SYSTEM_ADDRESS } else { ORDINARY };
    db.insert_account_info(caller, AccountInfo { balance: U256::from(10).pow(U256::from(18)), ..Default::default() });
    let salt_addr = match sc.kind { Kind::Sstore => CONTRACT, Kind::CallEmpty => EMPTY_TARGET };
    let envs = envs_for(sc.crowded, salt_addr);
    let mut context = MegaContext::new(db, SPEC).with_external_envs(envs.into());
    context.chain_mut().operator_fee_scalar = Some(U256::ZERO);
    context.chain_mut().operator_fee_constant = Some(U256::ZERO);
    let mut evm = MegaEvm::<_, NoOpInspector, _>::new(context);
    let tx_env = TxEnvBuilder::new().caller(caller).call(CONTRACT).gas_limit(GAS_LIMIT).value(U256::ZERO).data(Bytes::new()).build_fill();
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
