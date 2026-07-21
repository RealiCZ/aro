//! ARO differential probe — same deterministic `MultiPoint::open` workload as
//! `salt_multiproof_prove.rs`. Fingerprints ordered public queries/evaluations,
//! canonical proof bytes, successful verification, and rejection of a mutated claim.

use banderwagon::{CanonicalSerialize, Fr};
use ipa_multipoint::{
    crs::CRS,
    lagrange_basis::{LagrangeBasis, PrecomputedWeights},
    multiproof::{MultiPoint, MultiPointProof, ProverQuery, VerifierQuery},
    transcript::Transcript,
};
use sha2::{Digest, Sha256};

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

fn scalar_bytes(value: &Fr) -> [u8; 32] {
    let mut bytes = [0u8; 32];
    value
        .serialize_compressed(&mut bytes[..])
        .expect("serialize scalar");
    bytes
}

fn verify(proof: &MultiPointProof, workload: &Workload, queries: &[VerifierQuery]) -> bool {
    let mut transcript = Transcript::new(TRANSCRIPT_LABEL);
    proof.check(&workload.crs, &workload.precomp, queries, &mut transcript)
}

fn main() {
    let workload = workload();
    let verifier_queries = workload
        .queries
        .iter()
        .cloned()
        .map(VerifierQuery::from)
        .collect::<Vec<_>>();

    let mut prover_transcript = Transcript::new(TRANSCRIPT_LABEL);
    let proof = MultiPoint::open(
        workload.crs.clone(),
        &workload.precomp,
        &mut prover_transcript,
        workload.queries.clone(),
    );
    let proof_bytes = proof.to_bytes().expect("serialize canonical multiproof");
    let decoded = MultiPointProof::from_bytes(&proof_bytes, DOMAIN_SIZE)
        .expect("decode canonical multiproof");
    assert_eq!(decoded, proof, "canonical multiproof round trip");

    let verified = verify(&decoded, &workload, &verifier_queries);
    assert!(verified, "valid deterministic multiproof must verify");

    let mut mutated_queries = verifier_queries.clone();
    mutated_queries[0].result += Fr::from(1u64);
    let mutation_rejected = !verify(&decoded, &workload, &mutated_queries);
    assert!(
        mutation_rejected,
        "multiproof must reject a deterministic claimed-evaluation mutation"
    );

    let mut fingerprint = Sha256::new();
    fingerprint.update(b"aro-salt-multiproof-prove-diff-v1");
    fingerprint.update((verifier_queries.len() as u64).to_le_bytes());
    for query in &verifier_queries {
        fingerprint.update(query.commitment.to_bytes());
        fingerprint.update(scalar_bytes(&query.point));
        fingerprint.update(scalar_bytes(&query.result));
    }
    fingerprint.update((proof_bytes.len() as u64).to_le_bytes());
    fingerprint.update(&proof_bytes);
    fingerprint.update([verified as u8, mutation_rejected as u8]);

    print!("DIFF ");
    for byte in fingerprint.finalize() {
        print!("{byte:02x}");
    }
    println!();
}
