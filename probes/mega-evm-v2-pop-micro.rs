//! ARO isolation micro-bench for mega-evm's `compute_gas_ext::pop`.
//!
//! `pop` is the compute-gas-tracked wrapper mega-evm layers over revm's bare
//! `stack::pop` (see `crates/mega-evm/src/evm/instructions.rs`): it snapshots
//! `gas.remaining()`, runs the inner pop, then borrows the per-frame
//! `AdditionalLimit` and records the compute gas used via `record_compute_gas`.
//! In the parent workload (`probes/sweep_hotloop_v2.rs`) POP is emitted after
//! almost every opcode (`… ADD, POP`, `TIMESTAMP, POP`, `SLOAD, POP`,
//! `CALL, POP`), so its wrapper overhead is pervasive but individually far below
//! the parent bench's noise floor. This probe drives ONLY that wrapper in a
//! tight loop so a small real win (gas snapshot / RefCell borrow /
//! `record_compute_gas`) becomes resolvable.
//!
//! Isolation strategy: build one real `MegaContext` host (REX4 — the newest spec
//! the parent probe exercises) and one default `EthInterpreter`. Pre-fill the
//! interpreter stack from a fixed pool of realistic values, then drive `pop`
//! one call at a time over the full-to-empty stack. Refilling the stack is pure
//! setup and is kept OUTSIDE the timed region.
//!
//! `pop` is a tiny `#[inline]` fn that the optimizer would otherwise fold into
//! `main`, leaving the flat profiler (which attributes self-time to the
//! top-of-stack symbol, no inline resolution) unable to see it. So it is called
//! through a `black_box`'d function pointer: the indirect call cannot be inlined
//! or devirtualized, forcing the monomorphized `pop` to be emitted as its own
//! symbol that owns the drain's self-time. `record_compute_gas` and revm's inner
//! `stack::pop` still inline INTO that `pop` body, so its self-time reflects the
//! whole wrapper — exactly what the parent pays per POP.
//!
//! Modes (matching the other ARO probes):
//!   mega_evm_v2_pop_micro              bench: 5 samples of ns/pop -> `BENCH …`
//!                                      (inner reps scale with ARO_BENCH_SCALE)
//!   mega_evm_v2_pop_micro <spin_secs>  profile: spin in the drain loop until the
//!                                      deadline so the sampler can attach.

use std::hint::black_box;
use std::time::{Duration, Instant};

use mega_evm::alloy_primitives::U256;
use mega_evm::compute_gas_ext;
use mega_evm::revm::database::{CacheDB, EmptyDB};
use mega_evm::revm::interpreter::interpreter::EthInterpreter;
use mega_evm::revm::interpreter::{InstructionContext, Interpreter};
use mega_evm::{EmptyExternalEnv, MegaContext, MegaSpecId};

// REX4: the newest spec the parent probe (`sweep_hotloop_v2`) drives — its
// detention / dual-gas / limit machinery, incl. the compute-gas tracker `pop`
// records into, is live here.
const SPEC: MegaSpecId = MegaSpecId::REX4;

// Items pushed per refill == pop calls per timed chunk. revm's STACK_LIMIT is
// 1024; stay just under it so every refill push succeeds and the drain empties
// the stack exactly (every `pop` takes the normal record-compute-gas path, none
// hit the degenerate underflow path).
const FILL: usize = 1000;

// Concrete host + `pop` monomorphization the parent path uses.
type Host = MegaContext<CacheDB<EmptyDB>, EmptyExternalEnv>;
type PopFn = fn(InstructionContext<'_, Host, EthInterpreter>);

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

/// Build the fixed pool of realistic stack values (deterministic; done once,
/// outside every timed region). Mirror the values the parent workload feeds POP:
/// results of small ADDs (1..=16), storage slot ids, SLOAD values (~100-107),
/// CALL success flags (0/1), timestamps — overwhelmingly small integers, with
/// the occasional full-width word (hashes / addresses). So ~90% small integers,
/// ~10% wide words. (POP's cost is value-independent, but realistic contents keep
/// the bench honest and non-degenerate.)
fn build_pool() -> Vec<U256> {
    let mut rng = XorShift(0x9E37_79B9_7F4A_7C15);
    (0..FILL)
        .map(|_| {
            let r = rng.next();
            if r % 10 == 0 {
                U256::from(r).wrapping_mul(U256::from(rng.next()))
            } else {
                U256::from(r % 256)
            }
        })
        .collect()
}

/// Prime the interpreter stack ONCE with FILL realistic values (real pushes,
/// initializing the backing storage). Done during setup only.
fn prime_fill(interp: &mut Interpreter<EthInterpreter>, pool: &[U256]) {
    for v in pool {
        let _ = interp.stack.push(*v);
    }
}

/// Restore the stack to FILL items in O(1). revm's `pop` only decrements the
/// length (`set_len`/`Vec::pop`) and copies the value out — it never clears the
/// backing storage — so after a full drain the FILL `U256` words still sit in
/// slots `0..FILL`. Re-exposing them is a length reset, not FILL fresh pushes,
/// which keeps refill negligible so the timed/profiled region is essentially all
/// `pop`.
///
/// SAFETY: `U256` is a plain `Copy` POD (no `Drop`, no owned allocation), the
/// stack was primed to FILL (< STACK_LIMIT = capacity, per `Stack::new`), and
/// every intervening drain left exactly those FILL words initialized. So slots
/// `0..FILL` are valid, initialized `U256`s and `set_len(FILL)` reuses them.
#[inline]
fn refill(interp: &mut Interpreter<EthInterpreter>) {
    debug_assert!(interp.stack.data().capacity() >= FILL);
    unsafe {
        interp.stack.data_mut().set_len(FILL);
    }
}

/// Drive `pop` exactly FILL times over a full-to-empty stack, through the opaque
/// function pointer so the real `pop` symbol is exercised (not an inlined copy).
/// Unrolled 10x (FILL is a multiple of 10) so the loop-counter bookkeeping is
/// amortized and self-time concentrates in `pop` rather than the loop.
#[inline]
fn drain(pop_fn: PopFn, interp: &mut Interpreter<EthInterpreter>, host: &mut Host) {
    for _ in 0..(FILL / 10) {
        for _ in 0..10 {
            let ctx = InstructionContext { interpreter: &mut *interp, host: &mut *host };
            pop_fn(ctx);
        }
    }
}

fn main() {
    let spin_secs: Option<u64> = std::env::args().nth(1).and_then(|s| s.parse().ok());
    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);

    let pool = build_pool();

    // Host: a fresh REX4 MegaContext. It owns the `AdditionalLimit` the `pop`
    // wrapper borrows and records compute gas into. Reused across all calls.
    let db = CacheDB::<EmptyDB>::default();
    let mut host = MegaContext::new(db, SPEC);

    // Interpreter: default Eth wiring (u64::MAX gas, empty stack, no bytecode
    // error). Reused across all calls; refilled between drains.
    let mut interp: Interpreter<EthInterpreter> = Interpreter::default_ext();

    // Opaque function pointer to the target monomorphization: black_box stops the
    // optimizer from inlining/devirtualizing it, so each call lands in the real
    // `pop` symbol the profiler can attribute self-time to.
    let pop_fn: PopFn = compute_gas_ext::pop::<EthInterpreter, Host>;
    let pop_fn = black_box(pop_fn);

    // Prime the stack once with realistic values; later refills are O(1) resets.
    prime_fill(&mut interp, &pool);

    let mut acc: u64 = 0;

    if let Some(secs) = spin_secs {
        // Profile mode: stay in the drain loop until the deadline so the sampler
        // (which runs `binary <spin_secs>`) can attach. Scale-independent.
        let deadline = Instant::now() + Duration::from_secs(secs.max(1));
        let mut n: u64 = 0;
        while Instant::now() < deadline {
            for _ in 0..64 {
                refill(&mut interp);
                drain(pop_fn, &mut interp, &mut host);
                acc = acc.wrapping_add(interp.gas.remaining());
                n += 1;
            }
        }
        black_box(acc);
        println!("SPUN {n} drains in {secs}s");
        return;
    }

    // Bench mode: 5 samples of ns per `pop`. Refill (setup) is excluded from the
    // timed region; higher ARO_BENCH_SCALE averages more drains per sample.
    let chunks_per_sample: u64 = 8 * scale;

    // Warmup (excluded from samples).
    for _ in 0..2 {
        refill(&mut interp);
        drain(pop_fn, &mut interp, &mut host);
        acc = acc.wrapping_add(interp.gas.remaining());
    }

    let mut samples: Vec<f64> = Vec::with_capacity(5);
    for _ in 0..5 {
        let mut elapsed_ns = 0.0f64;
        let mut calls = 0u64;
        for _ in 0..chunks_per_sample {
            // Setup: O(1) stack length reset (NOT timed).
            refill(&mut interp);
            // Timed region: FILL `pop` calls, nothing else of substance.
            let t = Instant::now();
            drain(pop_fn, &mut interp, &mut host);
            elapsed_ns += t.elapsed().as_nanos() as f64;
            calls += FILL as u64;
            // Consume observable state so the drain cannot be optimized away.
            acc = acc
                .wrapping_add(interp.stack.len() as u64)
                .wrapping_add(interp.gas.remaining());
        }
        samples.push(elapsed_ns / calls as f64);
    }

    black_box(acc);
    let line = samples
        .iter()
        .map(|s| format!("{s:.4}"))
        .collect::<Vec<_>>()
        .join(" ");
    println!("BENCH {line}");
}
