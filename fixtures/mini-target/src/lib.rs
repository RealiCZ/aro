//! ARO fixture crate: a deliberately naive kernel with a known, byte-identical,
//! order-of-magnitude optimization (hoist the `base` accumulation out of the outer
//! loop — it does not depend on `i`). The E2E test seeds exactly that patch through
//! the full real judge (worktree → build → test → differential → A/A + A/B).

pub fn checksum(xs: &[u64]) -> u64 {
    let mut acc = 0u64;
    for i in 0..xs.len() {
        let mut base = 0u64;
        for j in 0..xs.len() {
            base = base.wrapping_add(xs[j] ^ (j as u64));
        }
        acc = acc.wrapping_add(base.rotate_left((i % 63) as u32) ^ xs[i]);
    }
    acc
}

#[cfg(test)]
mod tests {
    use super::checksum;

    #[test]
    fn empty_is_zero() {
        assert_eq!(checksum(&[]), 0);
    }

    #[test]
    fn golden_small() {
        // base = (0^0) + (0^1) = 1; acc = rot(1,0)^0 + rot(1,1)^0 = 1 + 2 = 3.
        assert_eq!(checksum(&[0, 0]), 3);
    }
}
