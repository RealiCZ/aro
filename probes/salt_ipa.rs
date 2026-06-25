//! ARO benchmark probe — ipa-multipoint top-level `CRS::commit_lagrange_poly`
//! (a Verkle polynomial commitment). This is a WHOLE-STACK workload: it drives
//! ipa-multipoint's commit logic → banderwagon's multi-scalar-mul → arkworks
//! field/curve ops. Builds the CRS (256 bases) and the polynomial ONCE, then times
//! many commits, so the timed work is the MSM, not setup.
//!
//! Modes:
//!   `salt_ipa`         -> timed bench, prints `BENCH <ns_per_call> iters=.. scale=..`
//!   `salt_ipa <secs>`  -> spins the commit for <secs> (for the profiler/sampler)
//! Honors ARO_BENCH_SCALE (multiplies the timed commit count).

use banderwagon::Fr;
use ipa_multipoint::crs::CRS;
use ipa_multipoint::lagrange_basis::LagrangeBasis;
use std::time::Instant;

fn main() {
    let crs = CRS::default(); // precomputed 256-base verkle CRS
    let n = crs.max_number_of_elements();
    let values: Vec<Fr> = (0..n as u64)
        .map(|i| Fr::from(i.wrapping_mul(2654435761).wrapping_add(12345)))
        .collect();
    let poly = LagrangeBasis::new(values);

    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let secs: f64 = args[1].parse().unwrap_or(8.0);
        let t = Instant::now();
        while t.elapsed().as_secs_f64() < secs {
            std::hint::black_box(crs.commit_lagrange_poly(&poly));
        }
        return;
    }

    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);
    let calls: u64 = 300 * scale; // ~0.19s/run at scale 1 (commit ≈ 0.65ms); bounded at scale 64
    for _ in 0..50 {
        std::hint::black_box(crs.commit_lagrange_poly(&poly));
    }
    let t = Instant::now();
    for _ in 0..calls {
        std::hint::black_box(crs.commit_lagrange_poly(&poly));
    }
    let ns = t.elapsed().as_nanos() as f64 / calls as f64;
    println!("BENCH {} ns_per_call iters={} scale={}", ns, calls, scale);
}
