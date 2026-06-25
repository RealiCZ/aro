//! ARO differential probe — proves byte-identical behaviour of `Committer::mul_index`.
//! Deterministic fixed inputs; XORs a fingerprint of the canonical commitments of the
//! outputs. Prints `DIFF <hex>`. A behaviour-changing optimization -> a different hex,
//! which fails the correctness gate before significance is even measured.

use banderwagon::salt_committer::Committer;
use banderwagon::{platform, Element, Fr};

fn main() {
    let n_bases = 256usize;
    let mut crs = Vec::with_capacity(n_bases);
    for i in 0..n_bases {
        crs.push(Element::prime_subgroup_generator() * Fr::from((i as u64) + 1));
    }
    let committer = Committer::new(&crs, platform::DEFAULT_PRECOMP_WINDOW_SIZE);
    let scalars: Vec<Fr> = (0..512u64)
        .map(|i| Fr::from(i.wrapping_mul(0x9E3779B1).wrapping_add(7)))
        .collect();
    let n_scalars = scalars.len();

    let mut outs = Vec::with_capacity(2000);
    for k in 0..2000usize {
        outs.push(committer.mul_index(&scalars[k % n_scalars], k % n_bases));
    }
    let mut fp = [0u8; 64];
    for c in Element::batch_to_commitments(&outs) {
        for i in 0..64 {
            fp[i] ^= c[i];
        }
    }
    print!("DIFF ");
    for b in fp {
        print!("{:02x}", b);
    }
    println!();
}
