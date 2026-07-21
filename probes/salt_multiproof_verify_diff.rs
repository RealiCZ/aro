//! ARO differential probe — same deterministic multiproof-verification workload as
//! `salt_multiproof_verify.rs`. Fingerprints canonical precomputed proof bytes and
//! ordered public queries, then checks valid acceptance and proof/query mutation rejection.

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
const TRANSCRIPT_LABEL: &[u8] = b"aro-salt-multiproof-verify-v1";

struct Workload {
    crs: CRS,
    precomp: PrecomputedWeights,
    proof: MultiPointProof,
    proof_bytes: Vec<u8>,
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
        proof_bytes,
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

fn verify(workload: &Workload, proof: &MultiPointProof, queries: &[VerifierQuery]) -> bool {
    let mut transcript = Transcript::new(TRANSCRIPT_LABEL);
    proof.check(&workload.crs, &workload.precomp, queries, &mut transcript)
}

fn main() {
    let workload = workload();

    let verified = verify(&workload, &workload.proof, &workload.queries);
    assert!(verified, "valid deterministic multiproof must verify");

    let mut mutated_queries = workload.queries.clone();
    mutated_queries[0].result += Fr::from(1u64);
    let query_mutation_rejected = !verify(&workload, &workload.proof, &mutated_queries);
    assert!(
        query_mutation_rejected,
        "multiproof must reject a deterministic claimed-evaluation mutation"
    );

    let mut mutated_proof_bytes = workload.proof_bytes.clone();
    let final_scalar_offset = mutated_proof_bytes.len() - 32;
    mutated_proof_bytes[final_scalar_offset..].copy_from_slice(&scalar_bytes(&Fr::from(1u64)));
    assert_ne!(
        mutated_proof_bytes, workload.proof_bytes,
        "deterministic proof mutation must change canonical bytes"
    );
    let mutated_proof = MultiPointProof::from_bytes(&mutated_proof_bytes, DOMAIN_SIZE)
        .expect("deterministically mutated proof remains canonically decodable");
    let proof_mutation_rejected = !verify(&workload, &mutated_proof, &workload.queries);
    assert!(
        proof_mutation_rejected,
        "multiproof must reject a deterministic canonical proof mutation"
    );

    let mut fingerprint = Sha256::new();
    fingerprint.update(b"aro-salt-multiproof-verify-diff-v1");
    fingerprint.update((workload.proof_bytes.len() as u64).to_le_bytes());
    fingerprint.update(&workload.proof_bytes);
    fingerprint.update((workload.queries.len() as u64).to_le_bytes());
    for query in &workload.queries {
        fingerprint.update(query.commitment.to_bytes());
        fingerprint.update(scalar_bytes(&query.point));
        fingerprint.update(scalar_bytes(&query.result));
    }
    fingerprint.update([
        verified as u8,
        query_mutation_rejected as u8,
        proof_mutation_rejected as u8,
    ]);

    print!("DIFF ");
    for byte in fingerprint.finalize() {
        print!("{byte:02x}");
    }
    println!();
}
