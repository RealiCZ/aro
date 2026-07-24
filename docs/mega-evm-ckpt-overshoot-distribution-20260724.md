# Checkpoint accounting overshoot distribution (B-class measurement)

**UTC date**: 2026-07-24 (updated with mainnet sample)  
**Baseline target**: `245476834741de1e1a615d22e6287621b64f30cb`  
**ARO branch**: `server/mega-evm-hwcounters`  
**Evidence**: `docs/data/mega-evm-ckpt-overshoot-20260724/`  
**Nature**: pure measurement; no mega-evm production source landed; no megaeth-labs writes.

## Direct conclusion

### Merged decision table

| source | variant | overshoot p50/p90/p99/max (gas, wt) | residual tax (ops) | recoverable 5–9% ceil |
|---|---|---|---|---|
| synth+probe | V1 | 45512 / — / 50002 / 50002 | 3.02% | 4.85–8.73% WP |
| synth+probe | V2 | 184 / — / 50002 / 50002 | 4.04% | 4.80–8.64% WP |
| synth+probe | V3 | 184 / — / 50002 / 50002 | 5.26% | 4.74–8.53% WP |
| **mainnet n=1000** | **V1** | **665 / 4325 / 26935 / 27151** | **0.71%** | **4.96–8.94% WP** |
| **mainnet n=1000** | **V2** | **57 / 168 / 6706 / 6772** | **7.80%** | **4.61–8.30% WP** |
| **mainnet n=1000** | **V3** | **60 / 168 / 6705 / 6771** | **14.39%** | **4.28–7.71% WP** |

### Two product questions (mainnet)

1. **Natural long straight segments?** Yes, but **below the 50k synthetic bound**. V1 max=**27151** gas, p99=**26935**; distance to 50k adversarial ≈ **22851** gas. Longest V1 segment still contains control-flow ops (JUMPDEST/JUMPI appear inside V1 body) — not a pure ADD-mill, but a long stretch without storage/call/volatile checkpoints.
2. **Support V1 default?** **Yes, with a raised typical p50 expectation.** Mainnet V1 p50=**665** vs probe-shaped ref **184** (~**3.6×**). Still order-of-hundreds, not tens of thousands. Residual packaging tax ops only **0.71%** → recoverable ceil **~4.96–8.94% WP**. V2/V3 cut p50 to **57/60** and max to ~**6772**, at **7.8% / 14.4%** residual ops tax.

**Updated default recommendation:** keep **V1** as default; set spec overshoot bound from mainnet p99/max with margin — e.g. **≥3e4 gas** covers observed max 27151 with headroom, or retain **5e4** if keeping synthetic mill as adversarial design case. V2 optional if product wants loop bodies ~6–7k max overshoot.

## Mainnet sampling window

| Item | Value |
|---|---|
| chainId | `0x10e6` (MegaETH mainnet) |
| latest at start | **22086184** |
| block range covered | **22085597 – 22086184** |
| blocks scanned | **600** |
| tx hash pool | **2009** |
| traced txs | **1000** |
| method | `eth_getBlockByNumber` + `debug_traceTransaction` (structLogs; memory/stack/storage off) |
| total opcodes | **9,737,035** |
| fail / unknown ops | none / none |

### Stability (V1 as n grows)

| n | p50 | p99 | max | tax_ops |
|---:|---:|---:|---:|---:|
| 100 | 642 | 22495 | 26942 | 0.765% |
| 200 | 642 | 17917 | 26942 | 0.738% |
| 500 | 642 | 17917 | 27007 | 0.731% |
| 1000 | 665 | 26935 | 27151 | 0.708% |

p50 stable ~642–665 from n=100→1000; p99/max settle near ~27k by n=1000.

## Mainnet longest segments

- **V1**: seg_gas=**27151**, n_ops=**7833**, top includes PUSH2/DUP2/JUMPDEST/PUSH1/POP/JUMPI (control-flow present inside V1 segment)
- **V2**: seg_gas=**6772**, n_ops=**1848**, mulmod/swap/dup heavy body between jumps
- **V3**: seg_gas=**6771**, n_ops=**1847**, similar to V2

## Method notes (mainnet leg)

- RPC credential via process env only; not written to evidence files (see secret scan).
- Traces cached under `mainnet/trace_cache/<txhash>.json` (opcode stream only).
- Same V1/V2/V3 checkpoint sets as synthetic leg.
- `ckpt_gas_mass_frac` ≈1.0 on mainnet is **not** packaging residual (CALL `gasCost` includes child stipend in geth structLogs); packaging residual uses **ops**.

## Representativeness boundary (updated)

- **Closed gap:** 1000 real MegaETH mainnet txs from recent blocks via dedicated read RPC.
- **Still not:** full historical epochs, MEV-extreme tails beyond this window, or builder-private flow.
- Synthetic `synth_straight_arith` 50k bound remains an **adversarial upper design case**; mainnet observed max ≈ **27k** in this window.
- Probe-shaped V1 p50=184 under-represents mainnet typical (665) but same order of magnitude.

## Prior synthetic section (summary)

| workload class | V1 max (synth) | note |
|---|---:|---|
| storage/call probe & lanes | ≤196 | V1 already short |
| jump loop | 45512 → V2/V3 80 | control-flow sensitive |
| straight arith mill | 50002 | adversarial ceiling case |

Full synthetic streams: `stream.jsonl` / `analysis/*`.

## V1.5 addendum (backward-jump settle)

Offline re-cut of the same mainnet 1000-tx cache. Full write-up: `docs/mega-evm-ckpt-v1_5-backedge-20260724.md` and `docs/data/mega-evm-ckpt-overshoot-20260724/v1_5/`.

| metric (mainnet) | value |
|---|---:|
| (a) backedge / ops | **1.91%** |
| (b) JUMP/JUMPI / ops | **7.10%** (V2 tax ref 7.80% includes V1 ckpts) |
| V1.5 p50/p90/p99/max | **194 / 317 / 6753 / 6861** |
| code-length bound violations | **0** / 250080 runs |
| recoverable point (L=0.25) | **4.81–8.65% WP** |

V1.5 collapses mainnet right tail from V1 max 27151 → **6861** (≈V2 max 6772) while full-settle tax stays **2.57%** vs V2 residual ops **7.80%**.

## Artifacts

- `mainnet/mainnet_analysis.json` — decision rows, stability, answers
- `mainnet/tx_index.json` — tx hashes + blocks (no RPC URL)
- `mainnet/trace_cache/` — per-tx opcode streams
- `mainnet/secret_scan.json` — credential scan result
- `merged_decision.json` — synth + mainnet tables
- `SHA256SUMS` — refreshed

## Self-cert

1. **Cleanup**: sampler finished; no orphan jobs; RPC env cleared after run in post-processing shell.
2. **Credential scan**: see `mainnet/secret_scan.json` (must be clean / hits=0).
3. **Identity**: ARO push uses aro PAT when provided; mega-putin unused.
4. **megaeth-labs**: zero remote writes; RPC eth_/debug_ read-only.
