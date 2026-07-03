//! ARO benchmark probe — banderwagon `salt_committer::Committer::mul_index` hot path
//! (precompute-table wNAF scalar multiply / point adds). Builds the precompute table
//! ONCE, then times many `mul_index` calls, so the timed work is the per-op scalar-mul
//! (the add path), not the one-time table build.
//!
//! Modes:
//!   `salt_msm`         -> timed bench, prints  `BENCH <ns_per_call> iters=.. scale=..`
//!   `salt_msm <secs>`  -> spins the same workload for <secs> (for the profiler/sampler)
//! Honors ARO_BENCH_SCALE: multiplies the timed call count, so a noise-limited verdict
//! can re-bench at higher power without changing the path or the inputs.

use banderwagon::salt_committer::Committer;
use banderwagon::{platform, Element, Fr};
use std::time::Instant;

fn setup() -> (Committer, Vec<Fr>, usize) {
    let n_bases = 256usize;
    let mut crs = Vec::with_capacity(n_bases);
    for i in 0..n_bases {
        crs.push(Element::prime_subgroup_generator() * Fr::from((i as u64) + 1));
    }
    let committer = Committer::new(&crs, platform::DEFAULT_PRECOMP_WINDOW_SIZE);
    let scalars: Vec<Fr> = (0..1024u64)
        .map(|i| Fr::from(i.wrapping_mul(2654435761).wrapping_add(12345)))
        .collect();
    (committer, scalars, n_bases)
}

fn main() {
    let (committer, scalars, n_bases) = setup();
    let n_scalars = scalars.len();
    let args: Vec<String> = std::env::args().collect();

    if args.len() > 1 {
        // profiling mode: spin the hot path for <secs> so the sampler can attribute it.
        let secs: f64 = args[1].parse().unwrap_or(8.0);
        let t = Instant::now();
        let mut k = 0usize;
        while t.elapsed().as_secs_f64() < secs {
            std::hint::black_box(committer.mul_index(&scalars[k % n_scalars], k % n_bases));
            k = k.wrapping_add(1);
        }
        return;
    }

    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);
    let calls: u64 = 50_000 * scale;
    for k in 0..5_000usize {
        std::hint::black_box(committer.mul_index(&scalars[k % n_scalars], k % n_bases));
    }
    let t = Instant::now();
    for k in 0..calls as usize {
        std::hint::black_box(committer.mul_index(&scalars[k % n_scalars], k % n_bases));
    }
    let ns = t.elapsed().as_nanos() as f64 / calls as f64;
    println!("BENCH {} ns_per_call iters={} scale={}", ns, calls, scale);
}
