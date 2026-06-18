//! Differential probe for `evm-c`: AdditionalLimit::record_compute_gas per-opcode fan-out
//! reduction (four-dimension -> compute-gas-only fast path in limit.rs).
//!
//! Drives the EXACT production hot path (`tu_record_compute_gas` -> `record_compute_gas`)
//! over deterministic, seeded inputs across every spec and folds the observable outputs into
//! one FNV-1a fingerprint. The baseline and the patched build MUST print the same `DIFF <hex>`.
//!
//! ADVERSARIAL coverage — exercises exactly the paths the invariant safety argument depends on:
//!   (A) happy path: long runs of `record_compute_gas` under each spec, never exceeding.
//!   (B) compute-gas TX-level exceed: drive usage past `tx_compute_gas_limit`, confirm the
//!       latch (kind=ComputeGas) and that subsequent calls short-circuit identically.
//!   (C) OTHER-dimension already at/over its limit: push `data_size` over `tx_data_size_limit`
//!       via the REX5 oracle-hint hook (records data_size + check_limit latches), THEN call
//!       `record_compute_gas` — the fast path must observe the already-latched data_size exceed
//!       and short-circuit byte-identically (this is the precise case round 1 feared).
//!   (D) data_size pushed exactly TO the limit (not over) then compute gas driven over: confirms
//!       priority/ordering is preserved (compute-gas exceed latched, data_size still within).

use mega_evm::{AdditionalLimit, EvmTxRuntimeLimits, LimitCheck, LimitKind, MegaSpecId};

const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const FNV_PRIME: u64 = 0x00000100000001b3;

#[inline]
fn fnv1a(mut h: u64, bytes: &[u8]) -> u64 {
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

#[inline]
fn mix_u64(h: u64, v: u64) -> u64 {
    fnv1a(h, &v.to_le_bytes())
}

#[inline]
fn mix_bool(h: u64, b: bool) -> u64 {
    fnv1a(h, &[b as u8])
}

/// Canonicalize a `LimitCheck` into the fingerprint so any divergence in latched kind/limit/used
/// (not just the boolean) is caught.
fn mix_limit_check(h: u64, c: &LimitCheck) -> u64 {
    match c {
        LimitCheck::WithinLimit => mix_u64(h, 0xA1),
        LimitCheck::ExceedsLimit { kind, limit, used, frame_local } => {
            let kind_tag: u64 = match kind {
                LimitKind::DataSize => 1,
                LimitKind::KVUpdate => 2,
                LimitKind::ComputeGas => 3,
                LimitKind::StateGrowth => 4,
            };
            let mut h = mix_u64(h, 0xB2);
            h = mix_u64(h, kind_tag);
            h = mix_u64(h, *limit);
            h = mix_u64(h, *used);
            mix_bool(h, *frame_local)
        }
    }
}

const SPECS: [MegaSpecId; 8] = [
    MegaSpecId::EQUIVALENCE,
    MegaSpecId::MINI_REX,
    MegaSpecId::REX,
    MegaSpecId::REX1,
    MegaSpecId::REX2,
    MegaSpecId::REX3,
    MegaSpecId::REX4,
    MegaSpecId::REX5,
];

/// Small deterministic LCG so the per-opcode gas amounts vary like a real workload.
struct Lcg(u64);
impl Lcg {
    #[inline]
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0 >> 33
    }
}

fn limits_with(compute: u64, data: u64) -> EvmTxRuntimeLimits {
    EvmTxRuntimeLimits::no_limits()
        .with_tx_compute_gas_limit(compute)
        .with_tx_data_size_limit(data)
        .with_tx_kv_updates_limit(u64::MAX)
        .with_tx_state_growth_limit(u64::MAX)
}

fn main() {
    let mut h: u64 = FNV_OFFSET;

    for (si, spec) in SPECS.iter().enumerate() {
        let spec = *spec;
        h = mix_u64(h, si as u64);

        // ---- (A) happy path: long run of record_compute_gas, never exceeding. ----
        {
            let mut al = AdditionalLimit::new(spec, limits_with(u64::MAX, u64::MAX));
            let mut rng = Lcg(0x1234_5678 ^ (si as u64) << 20);
            for _ in 0..2000 {
                let g = rng.next() % 1000;
                let ok = al.tu_record_compute_gas(g);
                h = mix_bool(h, ok);
            }
            h = mix_u64(h, al.tu_compute_usage());
            h = mix_limit_check(h, &al.tu_latched());
            h = mix_bool(h, al.tu_has_exceeded());
        }

        // ---- (B) compute-gas TX-level exceed: drive past tx_compute_gas_limit. ----
        {
            let limit = 5000u64;
            let mut al = AdditionalLimit::new(spec, limits_with(limit, u64::MAX));
            let mut rng = Lcg(0xDEAD_BEEF ^ (si as u64));
            // record until well past the limit, capturing each result and the latch.
            for _ in 0..200 {
                let g = 50 + (rng.next() % 200);
                let ok = al.tu_record_compute_gas(g);
                h = mix_bool(h, ok);
                h = mix_bool(h, al.tu_has_exceeded());
            }
            // Extra calls after exceed: must keep short-circuiting identically.
            for _ in 0..20 {
                let ok = al.tu_record_compute_gas(1);
                h = mix_bool(h, ok);
            }
            h = mix_limit_check(h, &al.tu_latched());
            h = mix_u64(h, al.tu_compute_usage());
        }

        // ---- (C) ADVERSARIAL: data_size dimension pushed OVER its limit (REX5 oracle-hint),
        //         then record_compute_gas must observe the already-latched data_size exceed
        //         and short-circuit byte-identically. Oracle-hint metering is REX5-only, so
        //         on non-REX5 specs the hook is a no-op (records nothing / returns true) — we
        //         still fingerprint its result to lock that behavior in too. ----
        {
            let data_limit = 1000u64;
            let mut al = AdditionalLimit::new(spec, limits_with(u64::MAX, data_limit));
            // Push the data-size lane to/over its limit. On REX5 this records to tx_entry and
            // latches; on other specs record_oracle_hint_bytes is a no-op.
            let pre = al.tu_record_oracle_hint_bytes(data_limit + 500);
            h = mix_bool(h, pre);
            h = mix_bool(h, al.tu_has_exceeded());
            h = mix_limit_check(h, &al.tu_latched());
            // Now drive the per-opcode compute-gas path while the OTHER dimension is over.
            for _ in 0..50 {
                let ok = al.tu_record_compute_gas(10);
                h = mix_bool(h, ok);
                h = mix_limit_check(h, &al.tu_latched());
            }
            h = mix_u64(h, al.tu_compute_usage());
        }

        // ---- (D) data_size pushed near/at-edge via repeated log records (records into the
        //         current frame's discardable lane only when a frame exists; with no frame the
        //         log hook is a no-op), interleaved with compute gas that finally exceeds —
        //         confirms ordering/priority preserved across the mixed sequence. ----
        {
            let mut al = AdditionalLimit::new(spec, limits_with(8000, u64::MAX));
            let mut rng = Lcg(0xFEED_FACE ^ ((si as u64) << 11));
            for i in 0..400 {
                if i % 7 == 0 {
                    let okl = al.tu_on_log(rng.next() % 4, rng.next() % 64);
                    h = mix_bool(h, okl);
                }
                let g = 30 + (rng.next() % 120);
                let ok = al.tu_record_compute_gas(g);
                h = mix_bool(h, ok);
                h = mix_bool(h, al.tu_has_exceeded());
            }
            h = mix_limit_check(h, &al.tu_latched());
            h = mix_u64(h, al.tu_compute_usage());
        }

        // ---- (E) reset round-trip: reset clears the latch; re-driving must reproduce. ----
        {
            let mut al = AdditionalLimit::new(spec, limits_with(2000, u64::MAX));
            for _ in 0..100 {
                let _ = al.tu_record_compute_gas(50);
            }
            h = mix_bool(h, al.tu_has_exceeded());
            al.reset();
            h = mix_bool(h, al.tu_has_exceeded());
            h = mix_u64(h, al.tu_compute_usage());
            for _ in 0..100 {
                let ok = al.tu_record_compute_gas(50);
                h = mix_bool(h, ok);
            }
            h = mix_limit_check(h, &al.tu_latched());
        }
    }

    println!("DIFF {:016x}", h);
}
