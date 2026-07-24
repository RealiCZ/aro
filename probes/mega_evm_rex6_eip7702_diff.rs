//! ARO Lane 4 differential oracle for REX6 EIP-7702 authority accounting.

use std::hint::black_box;

use alloy_eips::eip7702::{Authorization, RecoveredAuthority, RecoveredAuthorization};
use alloy_primitives::{address, keccak256, Address, Bytes, U256};
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

fn push_len(buf: &mut Vec<u8>, n: usize) {
    buf.extend_from_slice(&(n as u64).to_be_bytes());
}
fn push_bytes(buf: &mut Vec<u8>, bytes: &[u8]) {
    push_len(buf, bytes.len());
    buf.extend_from_slice(bytes);
}

fn encode_outcome(encoded: &mut Vec<u8>, sc: &Scenario) {
    push_bytes(encoded, sc.name.as_bytes());
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
    // preserve input order in fingerprint
    push_len(encoded, auths.len());
    for a in &auths {
        if let Some(auth) = a.authority() {
            encoded.extend_from_slice(auth.as_slice());
        } else {
            encoded.extend_from_slice(&[0u8; 20]);
        }
        encoded.extend_from_slice(&a.chain_id().to_be_bytes::<32>());
        encoded.extend_from_slice(&a.nonce().to_be_bytes());
        encoded.extend_from_slice(a.address().as_slice());
    }
    let tx_env = TxEnvBuilder::new()
        .caller(CALLER)
        .call(CALLEE)
        .gas_limit(GAS_LIMIT)
        .authorization_list_recovered(auths)
        .build_fill();
    let mut mega_tx = MegaTransaction::new(tx_env);
    mega_tx.enveloped_tx = Some(Bytes::new());
    let returned = match evm.transact(mega_tx) {
        Ok(r) => r,
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
            encoded.push(1);
            push_bytes(encoded, code.original_bytes().as_ref());
        } else {
            encoded.push(0);
        }
    }
}

fn main() {
    let mut encoded = Vec::new();
    push_bytes(&mut encoded, WORKLOAD_ID.as_bytes());
    push_bytes(&mut encoded, WORKLOAD_VERSION.as_bytes());
    for sc in scenarios() {
        encode_outcome(&mut encoded, &sc);
    }
    let digest = keccak256(encoded);
    black_box(digest);
    println!("DIFF {digest:x}");
}
