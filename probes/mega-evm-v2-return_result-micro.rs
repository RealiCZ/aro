//! ARO isolation micro-bench for revm's `EthFrame::return_result` as monomorphized
//! and driven by mega-evm's frame-return path.
//!
//! `return_result` (revm-handler `EthFrame::<EthInterpreter>::return_result`) is the
//! function that folds a finished child frame's `FrameResult` back into the parent
//! interpreter: it frees the child memory context, drains any context error, pushes
//! the call's success flag onto the parent stack, refunds the child's unspent gas
//! (`erase_cost`), copies the returned bytes into the parent's return-memory range,
//! and records the refund. mega-evm calls it once per returning frame from inside
//! `MegaEvm::frame_return_result` / `last_frame_result` (see
//! `crates/mega-evm/src/evm/execution.rs`), so in the parent workload
//! (`probes/sweep_hotloop_v2.rs`) it fires ~97 times per transaction — once for the
//! top-level frame and once for each of the 96 inner `CALL`s into the callee — yet
//! each individual call is far below the parent bench's noise floor. This probe
//! drives ONLY `return_result` in a tight loop so a small real win becomes
//! resolvable.
//!
//! Isolation strategy: build one real `MegaContext` host (REX4 — the newest spec the
//! parent probe exercises) and one default `EthFrame`/`EthInterpreter` as the parent
//! frame. Each call feeds a freshly-built `FrameResult::Call` outcome mirroring what
//! the parent's callee returns (see the input distribution below), then re-uses the
//! same parent frame. `return_result` pushes one item and refunds gas per call, so
//! the parent stack grows by one and its gas by the refunded amount; both are reset
//! in O(1) (`Stack::set_len(0)` + a fresh `Gas`) between drains, OUTSIDE the timed
//! region, so the timed/profiled region is essentially all `return_result`.
//!
//! `return_result` is a plain (non-`#[inline]`) generic fn, but a whole-program build
//! could still inline the monomorphization into the caller, leaving the flat profiler
//! (top-of-stack symbol, no inline resolution) unable to see it. So it is called
//! through a `black_box`'d function pointer: the indirect call cannot be inlined or
//! devirtualized, forcing the monomorphized `return_result` to be emitted as its own
//! symbol that owns the drain's self-time. Its inlined callees (`erase_cost`,
//! `Stack::push`, `free_child_context`, `set_buffer`, …) roll up into that self-time —
//! exactly the work the parent pays per returning frame.
//!
//! Input distribution (mirrors `sweep_hotloop_v2.rs`): every returning frame is a
//! successful `CALL` (`InstructionResult::Return`) with EMPTY output and a zero-length
//! return-memory range — the parent's `CALL`s all use `retLen = 0` and the callee
//! `STOP`s with no output, so `target_len == 0` and no bytes are copied. The only
//! varying field is the child's unspent gas: the parent forwards `0xFFFF` (65535) to a
//! cheap callee, so ~99% of returns leave ~58k–65k gas; ~1% model an outer/top-level
//! frame with a large remainder (1M–29M). The refund path (`is_ok`) is taken every
//! time. `return_result`'s cost is value-independent, but realistic contents keep the
//! bench honest and non-degenerate.
//!
//! Modes (matching the other ARO probes):
//!   mega_evm_v2_return_result_micro              bench: 5 samples of ns/call -> `BENCH …`
//!                                                (inner reps scale with ARO_BENCH_SCALE)
//!   mega_evm_v2_return_result_micro <spin_secs>  profile: spin in the drain loop until
//!                                                the deadline so the sampler can attach.

use std::convert::Infallible;
use std::hint::black_box;
use std::string::String;
use std::time::{Duration, Instant};

use mega_evm::alloy_primitives::Bytes;
use mega_evm::revm::context_interface::result::FromStringError;
use mega_evm::revm::database::{CacheDB, EmptyDB};
use mega_evm::revm::handler::{EthFrame, FrameResult};
use mega_evm::revm::interpreter::interpreter::EthInterpreter;
use mega_evm::revm::interpreter::{CallOutcome, Gas, InstructionResult, InterpreterResult};
use mega_evm::{EmptyExternalEnv, MegaContext, MegaSpecId};

// REX4: the newest spec the parent probe (`sweep_hotloop_v2`) drives — its detention /
// dual-gas / limit machinery is live here, and it is the spec whose `frame_return_result`
// calls `return_result` in the parent.
const SPEC: MegaSpecId = MegaSpecId::REX4;

// Returns driven per drain == `return_result` calls per timed chunk. Each call pushes
// one item onto the parent stack; revm's STACK_LIMIT is 1024, so stay under it (starting
// from an empty stack) so every push succeeds and every call takes the normal path.
// Multiple of 10 so the drain's inner unroll (`chunks_exact(10)`) divides evenly.
const FILL: usize = 1000;

// Concrete host + `return_result` monomorphization the parent path uses.
type Host = MegaContext<CacheDB<EmptyDB>, EmptyExternalEnv>;
type RrFn = fn(&mut EthFrame<EthInterpreter>, &mut Host, FrameResult) -> Result<(), RrErr>;

/// A minimal error type satisfying `return_result`'s `ERROR: From<ContextTrDbError<CTX>>
/// + FromStringError` bound. `CacheDB<EmptyDB>`'s DB error is `Infallible`, so the `Db`
/// arm is never constructed; the `Str` arm is never hit because the host carries no
/// context error. It exists only to name a concrete monomorphization.
#[derive(Debug)]
enum RrErr {
    #[allow(dead_code)]
    Db(Infallible),
    #[allow(dead_code)]
    Str(String),
}
impl From<Infallible> for RrErr {
    fn from(e: Infallible) -> Self {
        RrErr::Db(e)
    }
}
impl FromStringError for RrErr {
    fn from_string(s: String) -> Self {
        RrErr::Str(s)
    }
}

/// Deterministic xorshift64 PRNG — no system randomness, fixed seed.
struct XorShift(u64);
impl XorShift {
    #[inline]
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
}

/// Build the fixed pool of realistic child-remaining-gas values (deterministic; done
/// once, outside every timed region). Mirrors what the parent's returning frames carry:
/// the callee is forwarded `0xFFFF` and spends only a couple thousand gas, so ~99% of
/// returns leave ~58k–65k; ~1% model an outer/top-level frame with a large remainder.
fn build_gas_pool() -> Vec<u64> {
    let mut rng = XorShift(0x9E37_79B9_7F4A_7C15);
    (0..FILL)
        .map(|_| {
            let r = rng.next();
            if r % 100 == 0 {
                // Rare outer/top-level frame: large unspent remainder (1M..=29M).
                1_000_000 + (r % 28_000_000)
            } else {
                // Common inner CALL return: ~58_000..=65_535 unspent of the forwarded 0xFFFF.
                58_000 + (r % 7_536)
            }
        })
        .collect()
}

/// Reset the parent frame to its pre-drain shape in O(1): drop the pushed success flags
/// (`set_len(0)`) and restore a fresh, bounded `Gas`. Pure setup — kept OUTSIDE every
/// timed region.
///
/// SAFETY: `U256` stack slots are plain `Copy` PODs (no `Drop`), and the stack was
/// allocated with STACK_LIMIT capacity by `EthFrame::default()`, so shrinking the length
/// to 0 is sound and reuses the backing storage.
#[inline]
fn refill(frame: &mut EthFrame<EthInterpreter>) {
    unsafe {
        frame.interpreter.stack.data_mut().set_len(0);
    }
    frame.interpreter.gas = Gas::new(0);
}

/// Drive `return_result` exactly FILL times over the reset parent frame, through the
/// opaque function pointer so the real `return_result` symbol is exercised (not an
/// inlined copy). The `chunks_exact(10)` inner loop is a fixed-size body the compiler
/// unrolls, so the loop-counter bookkeeping is amortized and self-time concentrates in
/// `return_result` rather than the driver loop.
#[inline]
fn drain(rr: RrFn, frame: &mut EthFrame<EthInterpreter>, host: &mut Host, gas_pool: &[u64]) {
    debug_assert_eq!(gas_pool.len(), FILL);
    for chunk in gas_pool.chunks_exact(10) {
        for &rem in chunk {
            // Mirror the parent's returning frame: successful CALL, empty output, zero
            // return-memory range, `rem` gas unspent. Empty `Bytes` allocates nothing.
            let fr = FrameResult::Call(CallOutcome::new(
                InterpreterResult::new(InstructionResult::Return, Bytes::new(), Gas::new(rem)),
                0..0,
            ));
            let _ = rr(&mut *frame, &mut *host, fr);
        }
    }
}

fn main() {
    let spin_secs: Option<u64> = std::env::args().nth(1).and_then(|s| s.parse().ok());
    let scale: u64 =
        std::env::var("ARO_BENCH_SCALE").ok().and_then(|s| s.parse().ok()).unwrap_or(1);

    let gas_pool = build_gas_pool();

    // Host: a fresh REX4 MegaContext. `return_result` drains its context error each call.
    let db = CacheDB::<EmptyDB>::default();
    let mut host = MegaContext::new(db, SPEC);

    // Parent frame: default Eth wiring (empty stack with STACK_LIMIT capacity, zero gas,
    // no child memory context, no error). Reused across all calls; reset between drains.
    let mut frame = EthFrame::<EthInterpreter>::default();

    // Opaque function pointer to the target monomorphization: black_box stops the
    // optimizer from inlining/devirtualizing it, so each call lands in the real
    // `return_result` symbol the profiler can attribute self-time to.
    let rr: RrFn = EthFrame::<EthInterpreter>::return_result::<Host, RrErr>;
    let rr = black_box(rr);

    let mut acc: u64 = 0;

    if let Some(secs) = spin_secs {
        // Profile mode: stay in the drain loop until the deadline so the sampler (which
        // runs `binary <spin_secs>`) can attach. Scale-independent.
        let deadline = Instant::now() + Duration::from_secs(secs.max(1));
        let mut n: u64 = 0;
        while Instant::now() < deadline {
            for _ in 0..64 {
                refill(&mut frame);
                drain(rr, &mut frame, &mut host, &gas_pool);
                acc = acc.wrapping_add(frame.interpreter.gas.remaining());
                n += 1;
            }
        }
        black_box(acc);
        println!("SPUN {n} drains in {secs}s");
        return;
    }

    // Bench mode: 5 samples of ns per `return_result`. Refill (setup) is excluded from
    // the timed region; higher ARO_BENCH_SCALE averages more drains per sample.
    let chunks_per_sample: u64 = 8 * scale;

    // Warmup (excluded from samples).
    for _ in 0..2 {
        refill(&mut frame);
        drain(rr, &mut frame, &mut host, &gas_pool);
        acc = acc.wrapping_add(frame.interpreter.gas.remaining());
    }

    let mut samples: Vec<f64> = Vec::with_capacity(5);
    for _ in 0..5 {
        let mut elapsed_ns = 0.0f64;
        let mut calls = 0u64;
        for _ in 0..chunks_per_sample {
            // Setup: O(1) frame reset (NOT timed).
            refill(&mut frame);
            // Timed region: FILL `return_result` calls, nothing else of substance.
            let t = Instant::now();
            drain(rr, &mut frame, &mut host, &gas_pool);
            elapsed_ns += t.elapsed().as_nanos() as f64;
            calls += FILL as u64;
            // Consume observable state so the drain cannot be optimized away.
            acc = acc
                .wrapping_add(frame.interpreter.stack.len() as u64)
                .wrapping_add(frame.interpreter.gas.remaining());
        }
        samples.push(elapsed_ns / calls as f64);
    }

    black_box(acc);
    let line = samples.iter().map(|s| format!("{s:.4}")).collect::<Vec<_>>().join(" ");
    println!("BENCH {line}");
}
