//! ARO differential probe — proves byte-identical behaviour of the whole commit stack
//! (`CRS::commit_lagrange_poly` → banderwagon MSM). Deterministic: 64 fixed polynomials,
//! XORs a fingerprint of the canonical commitments. Prints `DIFF <hex>`; any behaviour
//! change → a different hex, failing the correctness gate before significance.

use banderwagon::{Element, Fr};
use ipa_multipoint::crs::CRS;
use ipa_multipoint::lagrange_basis::LagrangeBasis;

fn main() {
    let crs = CRS::default();
    let n = crs.max_number_of_elements();
    let mut fp = [0u8; 64];
    for j in 0..64u64 {
        let values: Vec<Fr> = (0..n as u64)
            .map(|i| Fr::from(i.wrapping_mul(0x9E3779B1).wrapping_add(j * 7 + 1)))
            .collect();
        let c = crs.commit_lagrange_poly(&LagrangeBasis::new(values));
        let bytes = Element::batch_to_commitments(&[c]);
        for k in 0..64 {
            fp[k] ^= bytes[0][k];
        }
    }
    print!("DIFF ");
    for b in fp {
        print!("{:02x}", b);
    }
    println!();
}
