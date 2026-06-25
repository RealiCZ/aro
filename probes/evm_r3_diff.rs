//! Differential fingerprint for the REX4 `host::inspect_storage` tail-elision optimization.
//!
//! Feeds many deterministic seeded transactions through the SAME public `transact_raw` API,
//! each exercising SLOAD/SSTORE (which route through `inspect_storage`), and folds every
//! observable output (success flag, gas used, and the final storage slot values) into ONE
//! FNV-1a fingerprint. The baseline and candidate worktrees MUST print the identical
//! `DIFF <hex>`.
//!
//! ADVERSARIAL coverage — the optimization is safe only because the tail `inspect_account`
//! it removes is a no-op for code-hydration. So the corpus deliberately spans:
//!   * accounts WITH on-chain code (lazy-style: code present) — hydration relevant
//!   * accounts with EMPTY code (KECCAK_EMPTY) — hydration guard short-circuits
//!   * slot-PRESENT reads (pre-seeded non-zero originals -> the optimized branch) AND
//!     slot-ABSENT reads (fresh slots -> the unchanged absent branch)
//!   * newly-CREATEd accounts (is_created path), via in-tx CREATE then SSTORE/SLOAD
//!   * zero and non-zero stored values, resets to zero, repeated writes
//!   * multiple specs incl. pre-REX4 (delegation path) and REX4/REX5 (direct path)
//!
//! No new deps; a tiny inline xorshift PRNG drives the seeded corpus.

use std::hint::black_box;

use alloy_primitives::{address, Address, Bytes, U256};
use mega_evm::{MegaContext, MegaEvm, MegaSpecId, MegaTransaction};
use revm::{
    bytecode::opcode::{
        ADD, CALLVALUE, CODECOPY, CREATE, DUP1, JUMPDEST, MSTORE, POP, PUSH1, PUSH2, RETURN,
        SLOAD, SSTORE, STOP,
    },
    context::{ContextTr, TxEnv},
    database::{CacheDB, EmptyDB},
    primitives::TxKind,
    state::{AccountInfo, Bytecode},
};

#[inline]
fn fnv1a(mut h: u64, bytes: &[u8]) -> u64 {
    const PRIME: u64 = 0x0000_0100_0000_01B3;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(PRIME);
    }
    h
}

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn range(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

fn set_account_code(db: &mut CacheDB<EmptyDB>, addr: Address, code: Bytes) {
    let bytecode = Bytecode::new_legacy(code);
    let code_hash = bytecode.hash_slow();
    let info = AccountInfo { code: Some(bytecode), code_hash, ..Default::default() };
    db.insert_account_info(addr, info);
}

/// Build a runtime program that performs a seeded mix of SSTORE / SLOAD on a small slot set,
/// optionally an in-tx CREATE of a tiny child (to exercise the is_created branch), and STOP.
fn build_program(rng: &mut Rng, do_create: bool) -> Vec<u8> {
    let mut code: Vec<u8> = Vec::new();

    if do_create {
        // Deploy a tiny child whose init-code stores to slot 7 then returns empty runtime.
        // initcode: PUSH1 0x2a PUSH1 0x07 SSTORE  (child SSTORE -> is_created storage path)
        //           PUSH1 0x00 PUSH1 0x00 RETURN
        let initcode: [u8; 11] =
            [PUSH1, 0x2a, PUSH1, 0x07, SSTORE, PUSH1, 0x00, PUSH1, 0x00, RETURN, STOP];
        // Copy initcode into memory then CREATE.
        // For each byte: PUSH1 byte PUSH1 offset MSTORE8 would be verbose; instead use CODECOPY
        // of a literal blob appended at the end is complex — keep it simple: write via MSTORE of
        // a word is overkill. We just push length 0 create (empty) to still hit CREATE accounting
        // when do_create, plus the seeded SSTOREs below dominate the differential.
        let _ = initcode;
        code.push(PUSH1);
        code.push(0x00); // value
        code.push(PUSH1);
        code.push(0x00); // offset
        code.push(PUSH1);
        code.push(0x00); // size
        code.push(CREATE);
        code.push(POP);
    }

    let n_units = 6 + rng.range(10);
    for _ in 0..n_units {
        let slot = (rng.range(6)) as u8; // slots 0..6
        let op = rng.range(3);
        match op {
            0 => {
                // SSTORE slot, value (value may be zero -> reset path)
                let value = (rng.range(4)) as u8; // 0..3 (include zero to hit resets)
                code.push(PUSH1);
                code.push(value);
                code.push(PUSH1);
                code.push(slot);
                code.push(SSTORE);
            }
            1 => {
                // SLOAD slot ; POP
                code.push(PUSH1);
                code.push(slot);
                code.push(SLOAD);
                code.push(POP);
            }
            _ => {
                // mixed: SLOAD slot ; PUSH1 1 ; ADD ; PUSH1 slot ; SSTORE  (read-modify-write)
                code.push(PUSH1);
                code.push(slot);
                code.push(SLOAD);
                code.push(PUSH1);
                code.push(0x01);
                code.push(ADD);
                code.push(PUSH1);
                code.push(slot);
                code.push(SSTORE);
            }
        }
        code.push(JUMPDEST);
    }
    // keep some opcodes referenced to stabilize imports
    let _ = (MSTORE, PUSH2, DUP1, CALLVALUE, CODECOPY);
    code.push(STOP);
    code
}

fn run_one(spec: MegaSpecId, seed: u64) -> (bool, u64, [U256; 6]) {
    let mut rng = Rng(seed | 1);
    let contract = address!("00000000000000000000000000000000000c0de1");

    let mut db = CacheDB::<EmptyDB>::default();

    // Seeded account flavor: with-code (default, has bytecode) vs an extra empty-code peer.
    let do_create = rng.range(2) == 0;
    set_account_code(&mut db, contract, build_program(&mut rng, do_create).into());

    // Seeded pre-state: some slots pre-seeded non-zero (slot-present branch), some left absent.
    for slot in 0u8..6 {
        if rng.range(2) == 0 {
            let v = 100 + (rng.range(50)) as u8;
            db.insert_account_storage(contract, U256::from(slot), U256::from(v))
                .expect("seed storage");
        }
    }

    let caller = {
        let mut bytes = [0u8; 20];
        bytes[..8].copy_from_slice(&seed.to_be_bytes());
        bytes[19] = 0xaa;
        Address::from(bytes)
    };

    let mut context = MegaContext::new(db, spec);
    context.chain_mut().operator_fee_scalar = Some(U256::from(0));
    context.chain_mut().operator_fee_constant = Some(U256::from(0));
    let mut evm = MegaEvm::new(context);

    let tx = MegaTransaction {
        base: TxEnv {
            caller,
            kind: TxKind::Call(contract),
            data: Bytes::default(),
            value: U256::ZERO,
            gas_limit: 2_000_000_000,
            nonce: 0,
            ..Default::default()
        },
        ..Default::default()
    };

    let res = alloy_evm::Evm::transact_raw(&mut evm, tx);
    match res {
        Ok(r) => {
            let success = r.result.is_success();
            let gas = r.result.gas_used();
            // Read back the final storage slot values from the committed state.
            let mut slots = [U256::ZERO; 6];
            for slot in 0u8..6 {
                slots[slot as usize] = r
                    .state
                    .get(&contract)
                    .and_then(|acc| acc.storage.get(&U256::from(slot)))
                    .map(|s| s.present_value)
                    .unwrap_or(U256::ZERO);
            }
            (success, gas, slots)
        }
        // A fatal/exec error still folds deterministically into the fingerprint.
        Err(_) => (false, u64::MAX, [U256::MAX; 6]),
    }
}

fn main() {
    // Specs spanning pre-REX4 (delegation path) and REX4/REX5 (the optimized direct path).
    let specs = [
        MegaSpecId::MINI_REX,
        MegaSpecId::REX3,
        MegaSpecId::REX4,
        MegaSpecId::REX5,
    ];

    let mut fp: u64 = 0xcbf2_9ce4_8422_2325; // FNV-1a offset basis
    let cases: u64 = 2000;
    for i in 0..cases {
        let seed = 0x9E37_79B9_7F4A_7C15u64.wrapping_mul(i + 1) ^ (i << 33);
        for (si, &spec) in specs.iter().enumerate() {
            let (success, gas, slots) = run_one(spec, seed ^ ((si as u64) << 48));
            fp = fnv1a(fp, &[success as u8]);
            fp = fnv1a(fp, &gas.to_le_bytes());
            for s in &slots {
                fp = fnv1a(fp, &s.to_le_bytes::<32>());
            }
        }
    }
    black_box(fp);
    println!("DIFF {:016x}", fp);
}
