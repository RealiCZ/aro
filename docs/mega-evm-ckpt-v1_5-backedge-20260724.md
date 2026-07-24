# V1.5 (backward-jump settle) offline decision data

**UTC**: 2026-07-24  
**Nature**: offline re-analysis of cached traces; no RPC; no megaeth-labs writes.

## Definition

- Keep **V1** checkpoints (CALL-family, RETURN-class, storage-gas, volatile/SLOAD).
- **JUMP/JUMPI still wrapped** for direction.
- **Full settle only** when jump is **taken and target < current PC** (real backedge).
- Forward taken jumps and not-taken JUMPI: direction check only, no segment break / no full settle.

## 1) Back-jump frequency

| corpus | (a) backedge / all ops | (b) all JUMP/JUMPI / all ops | V2 tax ref |
|---|---:|---:|---:|
| mainnet n=1000 | **1.9114%** | **7.0994%** | 7.80% |
| synth+probe | **0.0000%** | **1.0184%** | — |

Mainnet detail: back=186025, taken=549002, forward_taken=362977, not_taken_JUMPI=141930, ops=9732322.
Synth note: pc reconstructed without stack; unreliable_jumps=500; backedge **under-approximated** after first JUMP in a frame — treat mainnet as authoritative for (a).

## 2) V1.5 segment length (gas-weighted)

| source | variant | p50 / p90 / p99 / max | n_segments |
|---|---|---|---:|
| mainnet | V1.5 | **194** / **317** / **6753** / **6861** | 250080 |
| synth+probe | V1.5 | 45512 / 50002 / 50002 / 50002 | 1303 |

Compare mainnet V1 (prior): p50=665 / p99=26935 / max=27151; V2: p50=57 / max=6772.
V1.5 mainnet sits between V1 and V2 on the right tail: max **6861** (vs V1 27151 / V2 6772), p50 **194** (vs V1 665 / V2 57).

## 3) Code-length bound (forward-monotonicity)

Check: within each V1.5 segment, per constant-depth run, each `pc` appears ≤1 (no loop without backedge boundary).
- mainnet: checked_runs=**250080**, violations=**0**
- synth: checked_runs=**1303**, violations=**0** (pc reconstruction limited)
- **Mainnet: no counterexample** — bound holds on this corpus.

## 4) Residual tax & recoverable estimate

| quantity | mainnet |
|---|---:|
| V1 full-settle tax (ckpt ops) | 0.6598% |
| backedge full-settle add | 1.9114% |
| full-settle total (V1∪backedge) | 2.5712% |
| all JUMP/JUMPI (direction wrap) | 7.0994% |
| light-only (jump non-backedge) | 5.1879% |

Model: `residual_eff = v1_tax + back_freq·1 + (jump_freq−back_freq)·L`

| L (light/full) | residual_eff | recoverable ceil band |
|---:|---:|---|
| 0 | 2.5712% | 4.87–8.77% WP |
| 0.25 | 3.8682% | 4.81–8.65% WP |
| 0.5 | 5.1652% | 4.74–8.54% WP |
| 1 | 7.7592% | 4.61–8.30% WP |

**Point estimate L=0.25:** residual_eff=**3.868%**, recoverable **4.81–8.65% WP**.
Falls between prior mainnet V1 band (4.96–8.94%) and V2 (4.61–8.30%): point **4.81–8.65%** is slightly below V1 full packaging skip (because light jump wraps remain) and above V2 (because most jumps are not full settle / not all jumps break segments the V2 way).

## 5) Longest mainnet V1.5 segment

- tx=`0x20b1db220ee0acb7238ead236a38d067419a3b3bb1d0bbebaf92bafa6db4e8a3` gas=**6861** n_ops=**1871** top=[0x50×311, 0x09×309, 0x8a×308, 0x87×307, 0x98×298, 0x89×250, 0x84×16, 0x83×11]
- Pattern read: long stretch without V1 ckpt and without **backward** taken jumps; may still contain forward JUMP/JUMPI and JUMPDEST (not settle boundaries under V1.5).

## Decision table rows to merge

| source | variant | p50/p90/p99/max | full-settle tax | jump wrap freq | recoverable (L=0.25) |
|---|---|---|---:|---:|---|
| mainnet | V1.5 | 194/317/6753/6861 | 2.571% | 7.099% | 4.81–8.65% |
| synth | V1.5 | 45512/50002/50002/50002 | 3.025% | 1.018% | (pc-limited) |

## Self-cert

1. Cleanup: offline only; no RPC/orphan jobs.
2. Credential: no RPC URL/PAT in this analysis.
3. Identity: ARO docs push when PAT provided.
4. megaeth-labs: zero writes.

