//! Differential probe for the mega-evm limit-check hot path (round 2, evm-b).
//!
//! Feeds many deterministic pseudo-random transactions (fixed seed, inline
//! xorshift PRNG, no new deps) through the real public `execute_transaction`
//! API across every mega spec and a mix of workload shapes, then folds every
//! observable output into one FNV-1a/xor fingerprint.
//!
//! The folded outputs include the four MegaETH resource-usage counters
//! (`compute_gas_used`, `data_size`, `kv_updates`, `state_growth_used`), which
//! are produced by the exact trackers that `AdditionalLimit::check_limit` and
//! `FrameLimitTracker::exceeds_current_frame_limit` drive. Any behavioural
//! change in the patched function would shift the fingerprint.
//!
//! ## Adversarial coverage of the evm-b safety argument
//!
//! The patch replaces `persistent.checked_add(discardable).expect("overflow")`
//! in `exceeds_current_frame_limit` with `wrapping_add`, relying on the
//! invariant that a single frame's `persistent + discardable` never overflows
//! u64. To pin that the *value-producing* and *branch-taking* behaviour is
//! identical, this probe deliberately:
//!   * varies gas limits (incl. very tight ones) so many transactions hit
//!     per-frame and TX-level limits — exercising the `ExceedsLimit` arm where
//!     `used` flows into the returned struct;
//!   * emits nested CALLs / CREATEs so frames are pushed and popped, making the
//!     per-frame entry inspected by `exceeds_current_frame_limit` change between
//!     opcodes (so the inspected `persistent`/`discardable`/`refund` vary);
//!   * mixes storage writes *and* clear-to-zero (refund-producing) so the
//!     `used().saturating_sub(refund)` comparison sees nonzero refunds;
//!   * runs every spec, so REX4 per-frame and pre-REX4 TX-level paths both run.
//!
//! The invariant guarantees `checked_add` never overflows on realistic single-tx
//! usage, so `wrapping_add` yields the identical value and the fingerprint must
//! match baseline exactly.
//!
//! Prints exactly one line: `DIFF <hex>`.
#![allow(missing_docs)]

use std::hint::black_box;

use mega_evm::{
    alloy_primitives::{address, Address, Bytes, U256},
    revm::bytecode::opcode::{
        ADD, CALL, CALLDATASIZE, CREATE, GAS, LOG0, LOG1, MSTORE, POP, PUSH0, RETURN, SLOAD, SSTORE,
        TIMESTAMP,
    },
    revm::inspector::NoOpInspector,
    test_utils::{BytecodeBuilder, MemoryDatabase},
    EmptyExternalEnv, MegaContext, MegaEvm, MegaSpecId, MegaTransaction, MegaTransactionOutcome,
};
use revm::{context::result::ExecutionResult, context::tx::TxEnvBuilder};

const CALLER: Address = address!("0000000000000000000000000000000000100000");
const CONTRACT: Address = address!("0000000000000000000000000000000000100002");
const CALLEE: Address = address!("0000000000000000000000000000000000100003");

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

/// A small callee contract that does its own compute + storage + (sometimes)
/// nested CALL, so the limit trackers push/pop frames and accumulate per-frame
/// usage that `exceeds_current_frame_limit` inspects.
fn callee_code(rng: &mut Rng) -> Bytes {
    let mut b = BytecodeBuilder::default();
    let n = 1 + rng.range(5);
    for i in 0..n {
        b = b.push_number(rng.next() as u64).push_number(7u64).append(ADD).append(POP);
        // storage write then conditional clear (clear-to-zero produces a refund).
        b = b.push_number(i + 1).push_number(i).append(SSTORE);
        if rng.range(2) == 0 {
            b = b.push_number(0u64).push_number(i).append(SSTORE);
        }
    }
    // RETURN empty.
    b = b.push_number(0u64).push_number(0u64).append(RETURN);
    b.build()
}

/// Build a pseudo-random but deterministic top-level bytecode mixing compute,
/// storage, log, nested CALL/CREATE, and volatile-data opcodes — every category
/// routes through the limit trackers that `check_limit` /
/// `exceeds_current_frame_limit` read.
fn build_code(rng: &mut Rng) -> Bytes {
    let mut b = BytecodeBuilder::default();
    let blocks = 1 + rng.range(9);
    for _ in 0..blocks {
        match rng.range(8) {
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
                // storage reads + clear-to-zero (refund path)
                let n = 1 + rng.range(4);
                for i in 0..n {
                    b = b.push_number(i).append(SLOAD).append(POP);
                    b = b.push_number(0u64).push_number(i).append(SSTORE);
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
                b = b.append(CALLDATASIZE).append(POP);
            }
            5 => {
                // nested CALL to the callee contract: pushes/pops a tracker frame,
                // and (with value) records state growth / kv / data on the callee frame.
                // CALL(gas, addr, value, argsOffset, argsLen, retOffset, retLen)
                let value = rng.range(2); // sometimes value-transferring
                let gas = 1 + rng.range(200_000);
                b = b
                    .push_number(0u64) // retLen
                    .push_number(0u64) // retOffset
                    .push_number(0u64) // argsLen
                    .push_number(0u64) // argsOffset
                    .push_number(value) // value
                    .push_address(CALLEE) // addr
                    .push_number(gas) // gas
                    .append(CALL)
                    .append(POP);
            }
            6 => {
                // CREATE a tiny contract: pushes a CREATE frame (+1 state growth) and
                // exercises the create-frame per-frame budget path.
                // store a 1-byte STOP (0x00) in memory then CREATE(value, off, len).
                b = b.push_number(0u64).push_number(0u64).append(MSTORE);
                b = b
                    .push_number(1u64) // len
                    .push_number(0u64) // offset
                    .push_number(0u64) // value
                    .append(CREATE)
                    .append(POP);
            }
            _ => {
                // GAS opcode then POP
                b = b.append(GAS).append(POP).append(PUSH0).append(POP);
            }
        }
    }
    b.build()
}

fn run_one(spec: MegaSpecId, code: &Bytes, callee: &Bytes, gas_limit: u64) -> MegaTransactionOutcome {
    let mut context = MegaContext::new(
        MemoryDatabase::default()
            .account_code(CONTRACT, code.clone())
            .account_code(CALLEE, callee.clone())
            .account_balance(CALLER, U256::from(10).pow(U256::from(18)))
            .account_balance(CONTRACT, U256::from(10).pow(U256::from(9))),
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
        let callee = callee_code(&mut rng);
        let code = build_code(&mut rng);
        // Vary gas limit so some cases run to completion and some hit per-frame
        // / TX-level limits. Includes very tight limits to maximise exceed-arm
        // coverage of `exceeds_current_frame_limit`.
        // Keep above the intrinsic-gas validation floor (calldata + access list +
        // base cost), so transactions reach execution and exercise the in-execution
        // per-frame / TX-level limit paths rather than erroring at validation.
        let gas_limit = match rng.range(6) {
            0 => 90_000,
            1 => 150_000,
            2 => 500_000,
            3 => 1_000_000,
            4 => 100_000_000,
            _ => 10_000_000_000,
        };
        let out = run_one(spec, &code, &callee, gas_limit);
        fold_outcome(&mut acc, &out);
    }

    println!("DIFF {:016x}", black_box(acc));
}
