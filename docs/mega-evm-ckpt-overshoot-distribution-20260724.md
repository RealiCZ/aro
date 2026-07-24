# Checkpoint accounting overshoot distribution (B-class measurement)

**UTC date**: 2026-07-24  
**Baseline target**: `245476834741de1e1a615d22e6287621b64f30cb`  
**ARO branch**: `server/mega-evm-hwcounters`  
**Evidence**: `docs/data/mega-evm-ckpt-overshoot-20260724/`  
**Nature**: pure measurement; no mega-evm production source landed; no megaeth-labs writes.

## Direct conclusion

| variant | overshoot p50 / p99 / max (EVM gas, gas-weighted) | residual packaging tax (ops) | recoverable of 5–9% WP ceil |
|---|---|---|---|
| **V1** | **45512** / **50002** / **50002** | **3.02%** | **4.85–8.73%** WP |
| **V2** | **184** / **50002** / **50002** | **4.04%** | **4.80–8.64%** WP |
| **V3** | **184** / **50002** / **50002** | **5.26%** | **4.74–8.53%** WP |

**Design takeaways (for V1/V2/V3 choice + overshoot bound in spec):**

1. **On probe/lane-shaped storage+call workloads, V1 already keeps segments short** (sweep p50≈184 gas; SSTORE/LOG lanes p50≈6–112). Frequent non-negotiable checkpoints (SSTORE/LOG/CALL/SLOAD/volatile) dominate the stream.
2. **Catastrophic overshoot is straight-line compute without jumps** (`synth_straight_arith` max **50002** gas / 16001 ops). V2/V3 do **not** shrink that class — there is nothing to jump-split.
3. **V2/V3 matter for loop/control-flow**: `synth_jump_loop` V1 max **45512** → V3 max **80** (JUMPI+JUMPDEST settle). Pool gas-weighted p50 drops V1 **45512** → V2/V3 **184** because jump mass is re-split while storage lanes stay similar.
4. **Residual packaging tax is op-count, not EVM-gas mass.** Checkpoint ops are only **3.0–5.3%** of executions → almost all of the T2a **5–9%** packaging ceiling remains recoverable under V1 already (**~4.85–8.73% WP**). V3 costs +2.2pp more wrappers for modest overshoot control on jumpy code only. (EVM-gas mass at checkpoints is ~99% because SSTORE is expensive — that is **not** packaging residual.)
5. **Recommended default for spec discussion: V1** with explicit **overshoot upper bound ≥ 5e4 gas** (or workload-class bounds: storage/call ≤ ~200; pure compute mills require JUMP policy or accept large bound). Prefer **V2** only if product wants loop bodies settled without JUMPDEST tax; **V3** if basic-block halt locality is a hard requirement.

## Method (chosen + leave-trace)

| Item | Choice |
|---|---|
| Trace | Disposable worktree example `ckpt_overshoot_probe` + revm `Inspector` `step`/`step_end`; gas cost = Δ `gas.remaining()`; CALL/CREATE strip child `gas_limit` |
| Production tree | Untouched (isolated worktree only; removed after archive) |
| Overshoot proxy | Inter-checkpoint straight-line **segment gas**; gas-weighted p50/p90/p99/max = overshoot envelope if limit crossed uniformly in gas mass |
| Residual packaging tax | `#checkpoint_ops / #ops` (wrapper invocations). **Not** EVM-gas fraction |
| Recoverable ceil | `(1 - residual_tax_ops) × [5%, 9%]` from T2a packaging→0 ceiling |
| RPC / EEST | RPC rate-limited; EEST not bundled — boundary stated below |

### Checkpoint sets

- **V1**: CALL-family + RETURN-class (STOP/RETURN/REVERT/SELFDESTRUCT) + storage-gas (SSTORE/LOG*/CREATE*/SELFDESTRUCT) + volatile/detention (TIMESTAMP/NUMBER/COINBASE/… + **SLOAD** for oracle detention arming)
- **V2**: V1 + JUMP/JUMPI
- **V3**: V2 + JUMPDEST
- Non-negotiable in all: volatile/detention + storage-gas

## Workload inventory (complete)

| workload | steps | ok | note |
|---|---:|---|---|
| `sweep_hotloop_v2` | 8650 | True | ARO probe mirror (REX4) |
| `rex6_sstore_log` | 651 | True | lane1-shaped |
| `rex6_create_shaped` | 2664 | True | lane2-shaped CREATE |
| `rex6_selfdestruct_shaped` | 2961 | True | lane3-approx storage+volatile |
| `rex6_eip7702_shaped` | 35 | True | lane4-approx nested CALL (not full 7702) |
| `rex6_system_salt_shaped` | 1201 | True | lane5 ordinary caller (no system exemption) |
| `synth_straight_arith` | 16003 | True | no-jump compute stress |
| `synth_jump_loop` | 15004 | True | JUMPI loop ~500 |
| `synth_basic_blocks` | 1301 | True | 100 JUMPDESTs |
| `synth_nested_calls` | 219 | True | CALL tree |
| `synth_return_paths` | 410 | mixed | RETURN success + REVERT fail |

Total step events: **~49k** (post jump-loop fix). Stream: `docs/data/mega-evm-ckpt-overshoot-20260724/stream.jsonl`.

## Decision table (pooled, gas-weighted overshoot)

| variant | overshoot p50/p99/max (gas, wt) | residual tax (ops) | ckpt gas-mass | recoverable ceil band |
|---|---|---|---|---|
| V1 | 45512 / 50002 / 50002 | 3.02% | 98.95% | 4.85–8.73% WP |
| V2 | 184 / 50002 / 50002 | 4.04% | 98.99% | 4.80–8.64% WP |
| V3 | 184 / 50002 / 50002 | 5.26% | 98.99% | 4.74–8.53% WP |

CSV: `analysis/decision_table.csv`. Full JSON: `analysis/analysis.json`.

## Per-workload overshoot max (design extremes)

| workload | V1 max | V2 max | V3 max |
|---|---:|---:|---:|
| `rex6_create_shaped` | 363 | 363 | 363 |
| `rex6_eip7702_shaped` | 21 | 21 | 21 |
| `rex6_selfdestruct_shaped` | 112 | 112 | 112 |
| `rex6_sstore_log` | 11 | 11 | 11 |
| `rex6_system_salt_shaped` | 8 | 8 | 8 |
| `sweep_hotloop_v2` | 196 | 196 | 196 |
| `synth_basic_blocks` | 3400 | 3400 | 33 |
| `synth_jump_loop` | 45512 | 80 | 80 |
| `synth_nested_calls` | 226 | 226 | 226 |
| `synth_return_paths` | 558 | 558 | 558 |
| `synth_straight_arith` | 50002 | 50002 | 50002 |

## Longest-segment case study

- **All variants**: workload `synth_straight_arith`, seg_gas=**50002**, n_ops=**16001**, top=[PUSH1×8000, POP×2001, ADD×2000, MUL×2000, SUB×2000]
- **V1-only extreme**: `synth_jump_loop` seg_gas=**45512** until V2/V3 split at JUMPI/JUMPDEST → max **80**

**Pattern**: pure PUSH/ADD/MUL/SUB/POP after one TIMESTAMP is the textbook “compute mill” that V1 cannot see until STOP. Product implication: either accept ~5e4 gas overshoot bound, inject JUMPDEST policy for long bodies, or keep per-op accounting on suspected long-linear contracts (out of scope here).

## Distribution artifacts

- Gas-weighted histogram buckets: `analysis/analysis.json` → `distributions`
- Per-wl breakdown: `analysis/per_wl_V{1,2,3}.json`
- Probe source archive: `ckpt_overshoot_probe.rs` (evidence only)
- Analyzer: `analyze_ckpt_overshoot.py` + copy under evidence dir
- `SHA256SUMS` over evidence tree

## Representativeness boundary

Synthetic + ARO probe-shaped bytecode only; no mainnet replay (RPC rate-limited). REX6 7702/system paths approximated. Numbers are design-grade for **relative V1/V2/V3 ranking and order-of-magnitude overshoot bounds**, not absolute mainnet quantiles.

## Self-cert

1. **Cleanup**: disposable worktree removed after archiving probe source + streams; no orphan aro/valgrind/perf for this job.
2. **Credential scan**: no PAT in remote URL/config/report.
3. **Identity**: ARO `server/mega-evm-hwcounters` push uses aro PAT when provided; mega-putin unused.
4. **megaeth-labs**: zero remote writes.
