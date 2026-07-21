//! ARO benchmark probe — deterministic `MultiPointProof::check` verification.
//! A valid canonical multiproof and its ordered public queries are prepared before
//! timing so the measured region repeatedly executes the public verifier.

use banderwagon::Fr;
use ipa_multipoint::{
    crs::CRS,
    lagrange_basis::{LagrangeBasis, PrecomputedWeights},
    multiproof::{MultiPoint, MultiPointProof, ProverQuery, VerifierQuery},
    transcript::Transcript,
};
use std::time::Instant;

const DOMAIN_SIZE: usize = 256;
const POLYNOMIALS: usize = 16;
const QUERIES_PER_POLYNOMIAL: usize = 2;
const TRANSCRIPT_LABEL: &[u8] = b"aro-salt-multiproof-verify-v1";

struct Workload {
    crs: CRS,
    precomp: PrecomputedWeights,
    proof: MultiPointProof,
    queries: Vec<VerifierQuery>,
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
    let mut prover_queries = Vec::with_capacity(POLYNOMIALS * QUERIES_PER_POLYNOMIAL);

    for polynomial in 0..POLYNOMIALS {
        let poly = LagrangeBasis::new(
            (0..DOMAIN_SIZE)
                .map(|coefficient| seeded_value(polynomial, coefficient))
                .collect(),
        );
        let commitment = crs.commit_lagrange_poly(&poly);
        for opening in 0..QUERIES_PER_POLYNOMIAL {
            let point = (polynomial * 29 + opening * 113 + 17) % DOMAIN_SIZE;
            prover_queries.push(ProverQuery {
                commitment,
                result: poly.evaluate_in_domain(point),
                poly: poly.clone(),
                point,
            });
        }
    }

    let queries = prover_queries
        .iter()
        .cloned()
        .map(VerifierQuery::from)
        .collect::<Vec<_>>();
    let mut transcript = Transcript::new(TRANSCRIPT_LABEL);
    let generated = MultiPoint::open(crs.clone(), &precomp, &mut transcript, prover_queries);
    let proof_bytes = generated
        .to_bytes()
        .expect("serialize canonical multiproof");
    let proof = MultiPointProof::from_bytes(&proof_bytes, DOMAIN_SIZE)
        .expect("decode canonical multiproof");
    assert_eq!(
        proof.to_bytes().expect("re-serialize canonical multiproof"),
        proof_bytes,
        "canonical multiproof round trip"
    );

    Workload {
        crs,
        precomp,
        proof,
        queries,
    }
}

fn verify(workload: &Workload) -> bool {
    let mut transcript = Transcript::new(TRANSCRIPT_LABEL);
    workload.proof.check(
        &workload.crs,
        &workload.precomp,
        &workload.queries,
        &mut transcript,
    )
}

fn main() {
    let workload = workload();
    assert!(
        verify(&workload),
        "valid deterministic multiproof must verify"
    );

    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let secs: f64 = args[1].parse().unwrap_or(8.0);
        let start = Instant::now();
        while start.elapsed().as_secs_f64() < secs {
            std::hint::black_box(verify(std::hint::black_box(&workload)));
        }
        return;
    }

    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1);
    let calls = scale;

    let start = Instant::now();
    let mut all_valid = true;
    for _ in 0..calls {
        all_valid &= std::hint::black_box(verify(std::hint::black_box(&workload)));
    }
    let ns = start.elapsed().as_nanos() as f64 / calls as f64;
    assert!(all_valid, "all timed multiproof verifications must succeed");
    println!("BENCH {ns} ns_per_call iters={calls} scale={scale}");
}
