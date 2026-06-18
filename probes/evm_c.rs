//! Microbench probe for `evm-c`: AdditionalLimit::record_compute_gas (the per-opcode
//! compute-gas / limit-check hot path) in limit.rs.
//!
//! The change reduces the per-opcode `check_limit()` four-dimension fan-out to a single
//! compute-gas check (the only dimension `record_compute_gas` can change). This probe drives
//! `record_compute_gas` in a tight loop modelling the dominant real workload: a stream of
//! opcodes recording small compute-gas amounts while staying within all limits — exactly the
//! path that runs once per executed opcode.
//!
//! Usage:
//!   evm_c            -> timed microbench, prints `BENCH <ns> ns_per_call ...`
//!   evm_c <spin>     -> spins for <spin> seconds (so the CPU profiler can sample the hot path)

use std::time::Instant;

use mega_evm::{AdditionalLimit, EvmTxRuntimeLimits, MegaSpecId};

/// Build an `AdditionalLimit` for REX5 (the production default spec) with a compute-gas limit
/// high enough that the loop never trips the limit — modelling normal in-budget execution.
#[inline]
fn fresh() -> AdditionalLimit {
    let limits = EvmTxRuntimeLimits::no_limits()
        .with_tx_compute_gas_limit(u64::MAX)
        .with_tx_data_size_limit(u64::MAX)
        .with_tx_kv_updates_limit(u64::MAX)
        .with_tx_state_growth_limit(u64::MAX);
    AdditionalLimit::new(MegaSpecId::REX5, limits)
}

/// One batch of `n` per-opcode compute-gas records. Returns an accumulator so the optimizer
/// cannot elide the work.
#[inline(never)]
fn run_batch(al: &mut AdditionalLimit, n: u64) -> u64 {
    let mut acc: u64 = 0;
    let mut g: u64 = 3;
    for _ in 0..n {
        // Vary the gas amount a little (3,5,7,... wrapping) like a real opcode mix, but keep it
        // tiny so the loop stays in-budget for the whole run.
        let ok = al.tu_record_compute_gas(g);
        acc = acc.wrapping_add(ok as u64).wrapping_add(g);
        g = (g + 2) & 0x3f;
        if g == 0 {
            g = 3;
        }
    }
    acc
}

fn main() {
    let arg = std::env::args().nth(1);

    // Profiler mode: spin for N seconds driving the hot path so `sample` can attribute self-time.
    if let Some(a) = arg.as_deref() {
        if let Ok(spin_secs) = a.parse::<u64>() {
            let deadline = Instant::now() + std::time::Duration::from_secs(spin_secs);
            let mut sink: u64 = 0;
            // A fresh tracker periodically so usage doesn't run away; the limit is u64::MAX so it
            // never exceeds regardless, but resetting keeps numbers small & cache-hot like real txs.
            let mut al = fresh();
            let mut since_reset: u64 = 0;
            while Instant::now() < deadline {
                sink = sink.wrapping_add(run_batch(&mut al, 100_000));
                since_reset += 100_000;
                if since_reset >= 5_000_000 {
                    al.reset();
                    since_reset = 0;
                }
            }
            std::hint::black_box(sink);
            return;
        }
    }

    // Bench mode.
    const WARMUP: u64 = 2_000_000;
    const ITERS: u64 = 50_000_000;

    let mut al = fresh();
    std::hint::black_box(run_batch(&mut al, WARMUP));

    // Re-fresh so the timed region starts from a clean, representative state.
    let mut al = fresh();
    let mut sink: u64 = 0;
    let start = Instant::now();
    let mut done: u64 = 0;
    while done < ITERS {
        let chunk = 5_000_000u64;
        sink = sink.wrapping_add(run_batch(&mut al, chunk));
        done += chunk;
        // Keep usage from saturating semantics surprises: reset periodically (limit is MAX so
        // results are identical, this just mirrors real per-tx lifecycle and keeps it honest).
        al.reset();
    }
    let elapsed = start.elapsed();
    std::hint::black_box(sink);

    let ns_per_call = elapsed.as_nanos() as f64 / done as f64;
    println!(
        "BENCH {:.4} ns_per_call iters={} elapsed_ms={:.2} sink={}",
        ns_per_call,
        done,
        elapsed.as_secs_f64() * 1e3,
        sink & 0xffff,
    );
}
