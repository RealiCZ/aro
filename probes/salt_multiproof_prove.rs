//! ARO benchmark probe — deterministic `MultiPoint::open` proof generation.
//! CRS, seeded polynomials, commitments, evaluations, and owned query batches are
//! prepared before timing so the measured region is primarily multiproof proving.

use banderwagon::Fr;
use ipa_multipoint::{
    crs::CRS,
    lagrange_basis::{LagrangeBasis, PrecomputedWeights},
    multiproof::{MultiPoint, ProverQuery},
    transcript::Transcript,
};
use std::time::Instant;

const DOMAIN_SIZE: usize = 256;
const POLYNOMIALS: usize = 16;
const QUERIES_PER_POLYNOMIAL: usize = 2;
const TRANSCRIPT_LABEL: &[u8] = b"aro-salt-multiproof-prove-v1";

struct Workload {
    crs: CRS,
    precomp: PrecomputedWeights,
    queries: Vec<ProverQuery>,
}

fn seeded_value(polynomial: usize, coefficient: usize) -> Fr {
    let p = polynomial as u64 + 1;
    let i = coefficient as u64 + 1;
    Fr::from(
        i.wrapping_mul(0x9e37_79b9_7f4a_7c15)
            .rotate_left((polynomial % 63 + 1) as u32)
            ^ p.wrapping_mul(0xd6e8_feb8_6659_fd93)
            ^ i.wrapping_mul(p).wrapping_mul(0xa5a5_a5a5_a5a5_a5a5),
    )
}

fn workload() -> Workload {
    let crs = CRS::default();
    assert_eq!(crs.max_number_of_elements(), DOMAIN_SIZE);
    let precomp = PrecomputedWeights::new(DOMAIN_SIZE);
    let mut queries = Vec::with_capacity(POLYNOMIALS * QUERIES_PER_POLYNOMIAL);

    for polynomial in 0..POLYNOMIALS {
        let poly = LagrangeBasis::new(
            (0..DOMAIN_SIZE)
                .map(|coefficient| seeded_value(polynomial, coefficient))
                .collect(),
        );
        let commitment = crs.commit_lagrange_poly(&poly);
        for opening in 0..QUERIES_PER_POLYNOMIAL {
            let point = (polynomial * 29 + opening * 113 + 17) % DOMAIN_SIZE;
            queries.push(ProverQuery {
                commitment,
                result: poly.evaluate_in_domain(point),
                poly: poly.clone(),
                point,
            });
        }
    }

    Workload {
        crs,
        precomp,
        queries,
    }
}

fn prove(workload: &Workload, queries: Vec<ProverQuery>) {
    let mut transcript = Transcript::new(TRANSCRIPT_LABEL);
    std::hint::black_box(MultiPoint::open(
        workload.crs.clone(),
        &workload.precomp,
        &mut transcript,
        queries,
    ));
}

fn main() {
    let workload = workload();
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let secs: f64 = args[1].parse().unwrap_or(8.0);
        let start = Instant::now();
        while start.elapsed().as_secs_f64() < secs {
            prove(&workload, workload.queries.clone());
        }
        return;
    }

    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1);
    let calls = scale;

    prove(&workload, workload.queries.clone());
    let query_batches = (0..calls)
        .map(|_| workload.queries.clone())
        .collect::<Vec<_>>();
    let start = Instant::now();
    for queries in query_batches {
        prove(std::hint::black_box(&workload), queries);
    }
    let ns = start.elapsed().as_nanos() as f64 / calls as f64;
    println!("BENCH {ns} ns_per_call iters={calls} scale={scale}");
}
