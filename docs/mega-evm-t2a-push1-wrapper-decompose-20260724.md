# T2a: push1 wrapper overhead decomposition

**UTC date**: 2026-07-24  
**Branch**: `server/mega-evm-hwcounters` (evidence) + mega-evm-v2 pipeline (candidates)  
**Probe**: `sweep_hotloop_v2.dbg` scale=8  
**Evidence**: `docs/data/mega-evm-hwcounters-20260723/t2_push1_wrapper/`

## 1. Wrapper structure (source)

`wrap_op_compute_gas!(push1, …, instructions::stack::push::<1>)` expands to:

1. `gas_before = context.interpreter.gas.remaining()`
2. `run_inner_instruction_or_abort!(revm stack::push::<1>)` — **opcode body**
3. `gas_used = gas_before - remaining`
4. `additional_limit.borrow_mut()`
5. `compute_gas!(…)` → `AdditionalLimit::record_compute_gas` → **`check_limit`**

Source: `crates/mega-evm/src/evm/instructions.rs` (`wrap_op_compute_gas`, `compute_gas!`).

## 2. Measured split

### Whole-program push1 share

| Meter | push1 share |
|---|---:|
| HW cycles (prior study) | **18.27%** |
| HW instructions | **23.72%** |
| Callgrind Ir inclusive | **20.08%** (2.767e9 Ir) |

Evidence: prior hwcounter report; `t2_push1_wrapper/cg.annotate.txt` line push1 20.08%.

### Inside push1 — Callgrind file attribution

| Component | % of **program** Ir | Role |
|---|---:|---|
| `compute_gas_ext::push1` function (inclusive) | 20.08 | whole wrapper+body |
| `limit/frame_limit.rs` cost attributed under push1 | **2.76** | frame tracker side of compute gas |
| `limit/compute_gas.rs` under push1 | **1.76** | compute gas tracker |
| `AdditionalLimit::check_limit` (global) | **4.75** | shared by all opcodes |

**push1-attributable wrapper floor** (frame_limit + compute_gas + ~20% of global check_limit as volume share):

- Absolute program Ir: **~5.5%**  
- As fraction of push1 (20.08%): **~27%** of push1 Ir

### HW `perf annotate` on push1 (self samples sum 100%)

| Band | Approx % of push1 samples | Notes |
|---|---:|---|
| Limit/gas accounting (counter at `0x158`, cmov gas math, fail `jb` 25%) | **35–50** | includes cold OOG edge; hot path subset lower |
| Stack body (movups/stack pointer/immediate materialize) | **35–45** | revm `stack::push::<1>` |
| Prologue/epilogue (push/pop callee-saved) | **10–15** | shared frame |

Evidence: `t2_push1_wrapper/annotate_push1.txt` (top: `jb` 25.05%, push r15 7.26%, gas math / stack stores).

## 3. Theoretical wrapper-cut ceiling (the number)

Using **conservative** band (wrapper = **27–50%** of push1 cycles):

| Estimate | Whole-program cycle % if wrapper→0 |
|---|---:|
| Low (27% of 18.27) | **~4.9%** |
| Mid (40% of 18.27) | **~7.3%** |
| High (50% of 18.27) | **~9.1%** |

**Report number for seed planning: theoretical ceiling ≈ 5–9% of whole-program cycles** (best case eliminating all packaging without touching stack::push body).  
Realistic agentic win is a **fraction** of that (fast-path only, still correct on every halt edge).

## 4. Seed directions (for mega-evm-v2 agentic)

| ID | Direction | Risk to byte-identical DIFF |
|---|---|---|
| a | `record_compute_gas` / `check_limit` fast path: fewer branches, `likely` WithinLimit | Medium — halt edges must match |
| b | Reduce per-op borrow_mut / field layout of AdditionalLimit / compute tracker | Medium — layout-sensitive |
| c | Coalesce accounting across straight-line ops | **High** — Resource-Limit record order is behavioral; only if proven per-spec identical |

**Hard red lines**: all specs, halt sites, gas values, four-dimension record order; ship stops before PR.

## 5. Next

Drive mega-evm-v2 agentic with hot_path on `record_compute_gas` / `compute_gas_ext::push1`, seeds a→b; accept only Ir+floors pass then wall-clock counterbalance; no PR.

## 6. Self-cert

1. Cleanup: annotate/callgrind done  
2. Credentials: none  
3. Identity: aro at push  
4. megaeth-labs: zero writes this phase  
