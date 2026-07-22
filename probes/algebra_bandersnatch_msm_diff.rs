//! Deterministic differential for the Salt-shaped Bandersnatch MSM benchmark.
//!
//! It uses exactly the benchmark's 256 bases and eight scalar vectors, checks
//! checked-MSM/unchecked-MSM/reference equivalence, reruns the workload to prove
//! determinism, fingerprints canonical output encodings, and verifies that the
//! immutable inputs were not changed. A deliberate scalar mutation must change
//! the input fingerprint, proving that the mutation guard is effective.

use ark_ec::{AffineRepr, CurveGroup, PrimeGroup, ScalarMul, VariableBaseMSM};
use ark_ed_on_bls12_381_bandersnatch::{EdwardsProjective, Fr};
use ark_ff::{AdditiveGroup, PrimeField};
use ark_serialize::CanonicalSerialize;
use sha2::{Digest, Sha256};

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
    let affine_bases = EdwardsProjective::batch_convert_to_mul_base(bases);
    EdwardsProjective::msm(&affine_bases, scalars)
        .expect("the deterministic workload has one scalar per base")
}

fn canonical_bytes(value: &impl CanonicalSerialize) -> Vec<u8> {
    let mut bytes = Vec::new();
    value
        .serialize_compressed(&mut bytes)
        .expect("canonical serialization must succeed");
    bytes
}

fn hash_canonical(hasher: &mut Sha256, value: &impl CanonicalSerialize) {
    let bytes = canonical_bytes(value);
    hasher.update((bytes.len() as u64).to_le_bytes());
    hasher.update(bytes);
}

fn input_digest(bases: &[EdwardsProjective], scalar_vectors: &[Vec<Fr>]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"algebra-bandersnatch-msm-input-v1");
    hasher.update((bases.len() as u64).to_le_bytes());
    for base in bases {
        hash_canonical(&mut hasher, base);
    }
    hasher.update((scalar_vectors.len() as u64).to_le_bytes());
    for scalars in scalar_vectors {
        hasher.update((scalars.len() as u64).to_le_bytes());
        for scalar in scalars {
            hash_canonical(&mut hasher, scalar);
        }
    }
    hasher.finalize().into()
}

fn reference_msm(bases: &[EdwardsProjective], scalars: &[Fr]) -> EdwardsProjective {
    let affine_bases = EdwardsProjective::batch_convert_to_mul_base(bases);
    affine_bases
        .iter()
        .zip(scalars)
        .fold(EdwardsProjective::ZERO, |acc, (base, scalar)| {
            acc + base.mul_bigint(scalar.into_bigint())
        })
}

fn outputs(bases: &[EdwardsProjective], scalar_vectors: &[Vec<Fr>]) -> Vec<EdwardsProjective> {
    scalar_vectors
        .iter()
        .map(|scalars| run_once(bases, scalars))
        .collect()
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        out.push(DIGITS[(byte >> 4) as usize] as char);
        out.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

fn main() {
    let (bases, scalar_vectors) = workload();
    let input_before = input_digest(&bases, &scalar_vectors);

    let first = outputs(&bases, &scalar_vectors);
    let second = outputs(&bases, &scalar_vectors);
    assert_eq!(first, second, "identical deterministic workloads diverged");

    for ((scalars, checked), rerun) in scalar_vectors.iter().zip(&first).zip(&second) {
        let affine_bases = EdwardsProjective::batch_convert_to_mul_base(&bases);
        let unchecked = EdwardsProjective::msm_unchecked(&affine_bases, scalars);
        let reference = reference_msm(&bases, scalars);
        assert_eq!(*checked, unchecked, "checked and unchecked MSM disagree");
        assert_eq!(
            *checked, reference,
            "variable-base MSM disagrees with reference sum"
        );
        assert_eq!(canonical_bytes(checked), canonical_bytes(rerun));
    }

    assert_eq!(
        input_before,
        input_digest(&bases, &scalar_vectors),
        "MSM or normalization mutated its input vectors"
    );
    let mut mutated_scalars = scalar_vectors.clone();
    mutated_scalars[0][0] += Fr::from(1_u64);
    assert_ne!(
        input_before,
        input_digest(&bases, &mutated_scalars),
        "input mutation detector failed"
    );

    let mut output_hasher = Sha256::new();
    output_hasher.update(b"algebra-bandersnatch-msm-output-v1");
    output_hasher.update((first.len() as u64).to_le_bytes());
    for output in &first {
        hash_canonical(&mut output_hasher, output);
    }
    let output_digest: [u8; 32] = output_hasher.finalize().into();
    println!("DIFF {} input={}", hex(&output_digest), hex(&input_before));
}
