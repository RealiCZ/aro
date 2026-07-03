// ARO differential probe for the mini-target fixture: deterministic pseudo-random
// inputs (xorshift, fixed seed) → fold every checksum() into one fingerprint.
// Byte-identical behaviour ⇒ identical `DIFF <hex>` line in baseline and candidate.
fn main() {
    let mut s: u64 = 0xDEAD_BEEF_CAFE_F00D;
    let mut next = || {
        s ^= s << 13;
        s ^= s >> 7;
        s ^= s << 17;
        s
    };
    let mut fp: u64 = 0;
    for case in 0..64u64 {
        let len = 1 + (next() % 200) as usize;
        let xs: Vec<u64> = (0..len).map(|_| next()).collect();
        fp = fp.rotate_left(7) ^ mini_target::checksum(&xs).wrapping_add(case);
    }
    println!("DIFF {:016x}", fp);
}
