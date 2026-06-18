// ARO committer microbench — isolates `Committer::mul_index`, the fixed-base
// scalar-multiply kernel a profiler attributes ~76% of a SALT state-root update
// to (Plainshift's `committer_mul_index_latency`). The existing salt_trie bench
// only sees this diluted end-to-end; this measures the kernel directly.
//
// Run: prints `ARO_MULINDEX_SAMPLES <ns/mul_index> ...` (one sample per batch).
// With an integer arg N: after measuring, spins N seconds so an external sampler
// (macOS `sample`) can attach and attribute time to the inner asm multiplies.
use banderwagon::salt_committer::Committer;
use banderwagon::{platform, Element, Fr};
use std::time::Instant;

fn build() -> (Committer, Vec<Fr>) {
    // Production-shaped table: 256 bases, the default precompute window.
    let mut crs = Vec::with_capacity(256);
    for i in 0..256u64 {
        crs.push(Element::prime_subgroup_generator() * Fr::from(i + 1));
    }
    let committer = Committer::new(&crs, platform::DEFAULT_PRECOMP_WINDOW_SIZE);
    let scalars: Vec<Fr> = (0..256u64).map(|i| -Fr::from(i + 1)).collect();
    (committer, scalars)
}

fn batch(committer: &Committer, scalars: &[Fr]) -> Element {
    let mut acc = Element::zero();
    for (i, s) in scalars.iter().enumerate() {
        acc += committer.mul_index(s, i);
    }
    acc
}

fn main() {
    let (committer, scalars) = build();

    let mut acc = Element::zero();
    for _ in 0..50 {
        acc += batch(&committer, &scalars); // warm up
    }

    // Emit a sample distribution (each = ns/mul_index over one 256-call batch),
    // so the judge can estimate this bench's DRAM-bound noise and bootstrap a CI.
    let runs = 60;
    let mut per_call: Vec<f64> = Vec::with_capacity(runs);
    for _ in 0..runs {
        let t = Instant::now();
        acc += batch(&committer, &scalars);
        per_call.push(t.elapsed().as_nanos() as f64 / scalars.len() as f64);
    }
    std::hint::black_box(&acc);
    let mut line = String::from("ARO_MULINDEX_SAMPLES");
    for s in &per_call {
        line.push_str(&format!(" {:.3}", s));
    }
    println!("{line}");

    if let Some(secs) = std::env::args().nth(1).and_then(|s| s.parse::<u64>().ok()) {
        let t = Instant::now();
        while t.elapsed().as_secs() < secs {
            acc += batch(&committer, &scalars);
        }
        std::hint::black_box(&acc);
    }
}
