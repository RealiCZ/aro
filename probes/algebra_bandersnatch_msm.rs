//! ARO benchmark: Salt-shaped Bandersnatch variable-base MSM.
//!
//! The deterministic 256-point CRS and eight repeated scalar vectors are built
//! before timing. Every measured call mirrors `banderwagon::multi_scalar_mul`:
//! normalize the projective bases to affine form, then run checked variable-base
//! MSM. This is intentionally not arkworks' default microbenchmark.

use ark_ec::{PrimeGroup, ScalarMul, VariableBaseMSM};
use ark_ed_on_bls12_381_bandersnatch::{EdwardsProjective, Fr};
use std::time::Instant;

const BASE_COUNT: usize = 256;
const SCALAR_VECTOR_COUNT: usize = 8;

fn workload() -> (Vec<EdwardsProjective>, Vec<Vec<Fr>>) {
    let generator = EdwardsProjective::generator();
    let bases = (0..BASE_COUNT)
        .map(|i| generator * Fr::from(i as u64 + 1))
        .collect();
    let scalar_vectors = (0..SCALAR_VECTOR_COUNT)
        .map(|round| {
            (0..BASE_COUNT)
                .map(|i| {
                    Fr::from(
                        (i as u64)
                            .wrapping_mul(2_654_435_761)
                            .wrapping_add(round as u64 * 7 + 12_345),
                    )
                })
                .collect()
        })
        .collect();
    (bases, scalar_vectors)
}

#[inline(never)]
fn run_once(bases: &[EdwardsProjective], scalars: &[Fr]) -> EdwardsProjective {
    // Salt's banderwagon wrapper receives projective CRS points and performs this
    // normalization for each MSM invocation.
    let affine_bases = EdwardsProjective::batch_convert_to_mul_base(bases);
    EdwardsProjective::msm(&affine_bases, scalars)
        .expect("the deterministic workload has one scalar per base")
}

fn main() {
    let (bases, scalar_vectors) = workload();
    let args: Vec<String> = std::env::args().collect();

    if args.len() > 1 {
        let secs: f64 = args[1].parse().unwrap_or(8.0);
        let start = Instant::now();
        let mut call = 0usize;
        while start.elapsed().as_secs_f64() < secs {
            let _ = std::hint::black_box(run_once(
                std::hint::black_box(&bases),
                std::hint::black_box(&scalar_vectors[call % scalar_vectors.len()]),
            ));
            call = call.wrapping_add(1);
        }
        return;
    }

    let scale: usize = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1);
    assert!(matches!(scale, 1 | 8), "ARO_BENCH_SCALE must be 1 or 8");

    const SAMPLES: usize = 7;
    let mut ns_per_call = Vec::with_capacity(SAMPLES);
    for sample in 0..SAMPLES {
        let start = Instant::now();
        for call in 0..scale {
            let _ = std::hint::black_box(run_once(
                std::hint::black_box(&bases),
                std::hint::black_box(
                    &scalar_vectors[(sample * scale + call) % scalar_vectors.len()],
                ),
            ));
        }
        ns_per_call.push(start.elapsed().as_nanos() as f64 / scale as f64);
    }

    print!("BENCH");
    for sample in ns_per_call {
        print!(" {sample:.3}");
    }
    println!(
        " ns_per_call iters_per_sample={scale} scale={scale} bases={BASE_COUNT} vectors={SCALAR_VECTOR_COUNT}"
    );
}
