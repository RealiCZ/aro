use ark_ed_on_bls12_381_bandersnatch::{Fq, Fr};
use ark_ff::{batch_inversion, PrimeField};

const BASE_BATCHES: usize = 4;
const MUL_ADD_ITEMS: usize = 256;
const INDIVIDUAL_INVERSES: usize = 8;
const BATCH_INVERSION_ITEMS: usize = 128;
const WORKLOAD_SEED: u64 = 0x6a09_e667_f3bc_c909;

#[derive(Clone)]
struct FieldBatch<F: PrimeField> {
    mul_add_original: Vec<F>,
    mul_add_lhs: Vec<F>,
    mul_rhs: Vec<F>,
    add_rhs: Vec<F>,
    inverse_inputs: Vec<F>,
    inverse_outputs: Vec<F>,
    batch_original: Vec<F>,
    batch_values: Vec<F>,
}

#[derive(Clone)]
struct Workload {
    fq: Vec<FieldBatch<Fq>>,
    fr: Vec<FieldBatch<Fr>>,
}

#[derive(Clone, Copy)]
struct DeterministicRng(u64);

impl DeterministicRng {
    fn next_u64(&mut self) -> u64 {
        // SplitMix64: fixed, platform-independent test-data generation.
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }

    fn field<F: PrimeField>(&mut self) -> F {
        let mut bytes = [0u8; 32];
        for chunk in bytes.chunks_exact_mut(8) {
            chunk.copy_from_slice(&self.next_u64().to_le_bytes());
        }
        F::from_le_bytes_mod_order(&bytes)
    }

    fn nonzero_field<F: PrimeField>(&mut self) -> F {
        let value: F = self.field();
        if value.is_zero() {
            F::ONE
        } else {
            value
        }
    }
}

fn make_field_batch<F: PrimeField>(rng: &mut DeterministicRng) -> FieldBatch<F> {
    let mul_add_original = (0..MUL_ADD_ITEMS).map(|_| rng.field()).collect::<Vec<_>>();
    let mul_add_lhs = mul_add_original.clone();
    let mul_rhs = (0..MUL_ADD_ITEMS).map(|_| rng.field()).collect();
    let add_rhs = (0..MUL_ADD_ITEMS).map(|_| rng.field()).collect();
    let inverse_inputs = (0..INDIVIDUAL_INVERSES)
        .map(|_| rng.nonzero_field())
        .collect::<Vec<_>>();
    // Allocate result storage before measurement; run_field_batch only overwrites it.
    let inverse_outputs = vec![F::ZERO; INDIVIDUAL_INVERSES];
    let batch_original = (0..BATCH_INVERSION_ITEMS)
        .map(|i| {
            if i % 17 == 0 {
                F::ZERO
            } else {
                rng.nonzero_field()
            }
        })
        .collect::<Vec<_>>();
    let batch_values = batch_original.clone();
    FieldBatch {
        mul_add_original,
        mul_add_lhs,
        mul_rhs,
        add_rhs,
        inverse_inputs,
        inverse_outputs,
        batch_original,
        batch_values,
    }
}

fn make_workload(scale: usize) -> Workload {
    let batches = BASE_BATCHES.checked_mul(scale).expect("scale overflow");
    let mut fq_rng = DeterministicRng(WORKLOAD_SEED ^ 0x4651_5f42_4153_4500);
    let mut fr_rng = DeterministicRng(WORKLOAD_SEED ^ 0x4652_5f53_4341_4c41);
    Workload {
        fq: (0..batches)
            .map(|_| make_field_batch(&mut fq_rng))
            .collect(),
        fr: (0..batches)
            .map(|_| make_field_batch(&mut fr_rng))
            .collect(),
    }
}

#[inline(never)]
fn run_field_batch<F: PrimeField>(batch: &mut FieldBatch<F>) {
    for ((lhs, mul), add) in batch
        .mul_add_lhs
        .iter_mut()
        .zip(&batch.mul_rhs)
        .zip(&batch.add_rhs)
    {
        *lhs *= mul;
        *lhs += add;
    }
    for (output, input) in batch.inverse_outputs.iter_mut().zip(&batch.inverse_inputs) {
        *output = input
            .inverse()
            .expect("individual inversion input is nonzero");
    }
    batch_inversion(&mut batch.batch_values);
}

#[inline(never)]
fn run_workload(workload: &mut Workload) {
    // Interleave the two concrete 4-limb fields as Salt does across curve and IPA work.
    for (fq, fr) in workload.fq.iter_mut().zip(&mut workload.fr) {
        run_field_batch(fq);
        run_field_batch(fr);
    }
}

fn validate_field_batch<F: PrimeField>(batch: &FieldBatch<F>) {
    for (((original, mul), add), output) in batch
        .mul_add_original
        .iter()
        .zip(&batch.mul_rhs)
        .zip(&batch.add_rhs)
        .zip(&batch.mul_add_lhs)
    {
        assert_eq!(*output, *original * mul + add);
    }
    for (input, output) in batch.inverse_inputs.iter().zip(&batch.inverse_outputs) {
        assert_eq!(*input * output, F::ONE);
    }
    for (input, output) in batch.batch_original.iter().zip(&batch.batch_values) {
        if input.is_zero() {
            assert!(output.is_zero());
        } else {
            assert_eq!(*input * output, F::ONE);
        }
    }
}

fn validate_workload(workload: &Workload) {
    for batch in &workload.fq {
        validate_field_batch(batch);
    }
    for batch in &workload.fr {
        validate_field_batch(batch);
    }
}

fn bench_scale() -> usize {
    let scale = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(1);
    assert!(matches!(scale, 1 | 8), "ARO_BENCH_SCALE must be 1 or 8");
    scale
}

use std::{hint::black_box, time::Instant};

const SAMPLES: usize = 7;

fn main() {
    let scale = bench_scale();
    // Every deterministic input, owned batch, clone, and result buffer is prepared here.
    let mut samples = (0..SAMPLES)
        .map(|_| make_workload(scale))
        .collect::<Vec<_>>();
    let mut ns_per_batch = Vec::with_capacity(SAMPLES);

    for workload in &mut samples {
        let start = Instant::now();
        run_workload(black_box(workload));
        let elapsed = start.elapsed();
        black_box(&*workload);
        ns_per_batch.push(elapsed.as_nanos() as f64 / (BASE_BATCHES * scale) as f64);
        // Correctness validation is deliberately outside the measured interval.
        validate_workload(workload);
    }

    print!("BENCH");
    for sample in ns_per_batch {
        print!(" {sample:.3}");
    }
    println!(" ns_per_mixed_fq_fr_batch scale={scale}");
}
