// ARO differential probe — a random-input behaviour check for the committer.
//
// Runs a deterministic pseudo-random batch through `Committer::mul_index` (each
// scalar AND its negation, to exercise the negated-digit path that layout/precompute
// changes touch) and prints one stable fingerprint `ARO_DIFF <hex>` over every
// result's bytes. The judge runs this SAME probe in the baseline and the candidate
// worktrees and requires identical output — a byte-level behaviour guarantee beyond
// the unit tests, which is what consensus/crypto code needs before any speed counts.
//
// Deterministic by construction (fixed LCG seed), so baseline and candidate see
// identical inputs; the only thing that can change the fingerprint is a behaviour
// difference in the implementation under test.
use banderwagon::salt_committer::Committer;
use banderwagon::{platform, Element, Fr};

fn main() {
    let mut crs = Vec::with_capacity(256);
    for i in 0..256u64 {
        crs.push(Element::prime_subgroup_generator() * Fr::from(i + 1));
    }
    let committer = Committer::new(&crs, platform::DEFAULT_PRECOMP_WINDOW_SIZE);

    // LCG → pseudo-random scalars spanning many bit patterns; identical run-to-run
    // yet far more inputs than the bench's structured ones.
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut h: u64 = 0xcbf2_9ce4_8422_2325; // FNV-1a accumulator
    for i in 0..256usize {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let s = Fr::from(state);
        for e in &[committer.mul_index(&s, i), committer.mul_index(&(-s), i)] {
            for b in e.to_bytes().iter() {
                h ^= *b as u64;
                h = h.wrapping_mul(0x100_0000_01b3);
            }
        }
    }
    println!("ARO_DIFF {:016x}", h);
}
