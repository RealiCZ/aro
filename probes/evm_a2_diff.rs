//! Differential probe for the mega-evm limit-check hot path (round 2).
//!
//! Feeds many deterministic pseudo-random transactions (fixed seed, inline
//! xorshift PRNG, no new deps) through the real public `execute_transaction`
//! API across every mega spec and a mix of workload shapes, then folds every
//! observable output into one FNV-1a/xor fingerprint.
//!
//! The folded outputs include the four MegaETH resource-usage counters
//! (`compute_gas_used`, `data_size`, `kv_updates`, `state_growth_used`) which
//! are computed by the exact trackers that `AdditionalLimit::check_limit` and
//! `FrameLimitTracker::exceeds_current_frame_limit` drive — so any behavioral
//! change in the patched function would shift the fingerprint. Varying gas
//! limits makes some cases run to completion and others hit per-frame / TX-level
//! limits, exercising both the within-limit and exceed branches of
//! `exceeds_current_frame_limit`.
//!
//! Prints exactly one line: `DIFF <hex>`. Must be identical for baseline and
//! the candidate (the `used()`-hoist change recomputes the same value, so it is).
#![allow(missing_docs)]

use std::hint::black_box;

use mega_evm::{
    alloy_primitives::{address, Address, Bytes, U256},
    revm::bytecode::opcode::{ADD, GAS, LOG0, LOG1, POP, PUSH0, SLOAD, SSTORE, TIMESTAMP},
    revm::inspector::NoOpInspector,
    test_utils::{BytecodeBuilder, MemoryDatabase},
    EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId, MegaTransaction, MegaTransactionOutcome,
};
use revm::{context::result::ExecutionResult, context::tx::TxEnvBuilder};

const CALLER: Address = address!("0000000000000000000000000000000000100000");
const CONTRACT: Address = address!("0000000000000000000000000000000000100002");

const FNV_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

#[inline]
fn fnv_fold(acc: &mut u64, bytes: &[u8]) {
    for &b in bytes {
        *acc ^= u64::from(b);
        *acc = acc.wrapping_mul(FNV_PRIME);
    }
}

#[inline]
fn fold_u64(acc: &mut u64, v: u64) {
    fnv_fold(acc, &v.to_le_bytes());
}

/// Inline xorshift64* PRNG — deterministic, no deps.
struct Rng(u64);
impl Rng {
    #[inline]
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    #[inline]
    fn range(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

const SPECS: [MegaSpecId; 6] = [
    MegaSpecId::EQUIVALENCE,
    MegaSpecId::MINI_REX,
    MegaSpecId::REX2,
    MegaSpecId::REX3,
    MegaSpecId::REX4,
    MegaSpecId::REX5,
];

/// Build a pseudo-random but deterministic bytecode mixing compute, storage,
/// log, and volatile-data opcodes — every category routes through the limit
/// trackers that `check_limit` / `exceeds_current_frame_limit` read.
fn build_code(rng: &mut Rng) -> Bytes {
    let mut b = BytecodeBuilder::default();
    let blocks = 1 + rng.range(8);
    for _ in 0..blocks {
        match rng.range(6) {
            0 => {
                // compute: ADD/POP loop
                let n = 1 + rng.range(6);
                for _ in 0..n {
                    b = b.push_number(rng.next() as u64).push_number(3u64).append(ADD).append(POP);
                }
            }
            1 => {
                // storage writes
                let n = 1 + rng.range(4);
                for i in 0..n {
                    b = b.push_number(i + 1).push_number(i).append(SSTORE);
                }
            }
            2 => {
                // storage reads
                let n = 1 + rng.range(4);
                for i in 0..n {
                    b = b.push_number(i).append(SLOAD).append(POP);
                }
            }
            3 => {
                // log emissions
                b = b.push_number(0u64).push_number(0u64).append(LOG0);
                b = b.push_number(0xdead_beef_u64).push_number(32u64).push_number(0u64).append(LOG1);
            }
            4 => {
                // volatile data access (gas detention)
                b = b.append(TIMESTAMP).append(POP);
            }
            _ => {
                // GAS opcode then POP
                b = b.append(GAS).append(POP).append(PUSH0).append(POP);
            }
        }
    }
    b.build()
}

fn run_one(spec: MegaSpecId, code: &Bytes, gas_limit: u64) -> MegaTransactionOutcome {
    let mut context = MegaContext::new(
        MemoryDatabase::default()
            .account_code(CONTRACT, code.clone())
            .account_balance(CALLER, U256::from(10).pow(U256::from(18))),
        spec,
    );
    context.modify_chain(|chain| {
        chain.operator_fee_scalar = Some(U256::ZERO);
        chain.operator_fee_constant = Some(U256::ZERO);
    });
    let mut evm = MegaEvm::<_, NoOpInspector, EmptyExternalEnv>::new(context);
    let tx = TxEnvBuilder::new()
        .caller(CALLER)
        .call(CONTRACT)
        .gas_limit(gas_limit)
        .value(U256::ZERO)
        .data(Bytes::new())
        .build_fill();
    let mut mega_tx = MegaTransaction::new(tx);
    mega_tx.enveloped_tx = Some(Bytes::new());
    evm.execute_transaction(mega_tx).expect("mega transact")
}

fn fold_outcome(acc: &mut u64, out: &MegaTransactionOutcome) {
    // Result classification + gas + output bytes.
    match &out.result {
        ExecutionResult::Success { gas_used, output, .. } => {
            fold_u64(acc, 1);
            fold_u64(acc, *gas_used);
            fnv_fold(acc, output.data().as_ref());
        }
        ExecutionResult::Revert { gas_used, output } => {
            fold_u64(acc, 2);
            fold_u64(acc, *gas_used);
            fnv_fold(acc, output.as_ref());
        }
        ExecutionResult::Halt { reason, gas_used } => {
            fold_u64(acc, 3);
            fold_u64(acc, *gas_used);
            // Fold the halt reason's debug string (covers mega-specific reasons).
            fnv_fold(acc, format!("{reason:?}").as_bytes());
        }
    }
    // The four MegaETH resource-usage counters — direct outputs of the limit
    // trackers that `check_limit` / `exceeds_current_frame_limit` read.
    fold_u64(acc, out.data_size);
    fold_u64(acc, out.kv_updates);
    fold_u64(acc, out.compute_gas_used);
    fold_u64(acc, out.state_growth_used);
}

fn main() {
    let mut rng = Rng(0x1234_5678_9abc_def0);
    let mut acc = FNV_OFFSET;

    const CASES: usize = 4000;
    for _ in 0..CASES {
        let spec = SPECS[(rng.range(SPECS.len() as u64)) as usize];
        let code = build_code(&mut rng);
        // Vary gas limit so some cases run to completion and some hit limits.
        let gas_limit = match rng.range(4) {
            0 => 100_000,
            1 => 1_000_000,
            2 => 100_000_000,
            _ => 10_000_000_000,
        };
        let out = run_one(spec, &code, gas_limit);
        fold_outcome(&mut acc, &out);
    }

    println!("DIFF {:016x}", black_box(acc));
}
