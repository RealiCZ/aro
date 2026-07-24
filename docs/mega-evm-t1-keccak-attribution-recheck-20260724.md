# T1: Lane2 keccak attribution recheck

**UTC date**: 2026-07-24  
**Branch**: `server/mega-evm-hwcounters`  
**Probe**: `aro_rex6_lane2_create.dbg` @ baseline `2454768`  
**Evidence**: `docs/data/mega-evm-hwcounters-20260723/t1_keccak_recheck/`

## 1. Method availability

| Method | Result |
|---|---|
| AMD IBS (`ibs_op//p`) | **Unavailable**: no `/sys/.../ibs_*` PMU on this host |
| `cycles:pp` / `cycles:p` | **not supported** (perf 6.8.12 + EPYC 9754) |
| Fallback | High-freq `perf record -e cycles|instructions -F 20000` + **12s spin** (~119K samples) + **callgrind Ir** scale=8 |

Pin `taskset -c 2`, `RAYON_NUM_THREADS=1`. Host quiet before measure.

## 2. Prior report artifact — confirmed

| Source | cycles% | instructions% |
|---|---:|---:|
| Prior report (wrong) | **49.6** | **2.6** |
| Raw scale=8 file `keccak_p` | 46.79 (**35 samples**) | **64.30** (43 samples) |
| Raw `keccak256_impl` | 2.76 | 2.57 |

**Root causes (both)**:

1. **Undersampling**: scale=8 record had ~70 symbol samples; keccak only 35 points.
2. **demangle dict overwrite**: analysis built `ins = {short: row}`; both `keccak_p` and `keccak256_impl` collapsed to key `keccak`; **last wins** (`keccak256_impl` 2.57% overwrote `keccak_p` 64.30%). Cycles side aggregated to ~49.5%.

Resulting fake 49.6/2.6 implied rest IPC~6.5 — **tooling artifact**, not skid parking stalls on keccak.

## 3. Corrected measurement (spin12, F=20k, lost=0)

| Symbol | cycles% | samples | instructions% | samples | cyc/instr |
|---|---:|---:|---:|---:|---:|
| `keccak::backends::soft::keccak_p` | **46.59** | 55524 | **65.01** | 76978 | **0.72** |
| `alloy_primitives::keccak256_impl` | 4.33 | 5157 | 4.40 | 5180 | 0.98 |
| **family total** | **50.92** | — | **69.41** | — | 0.73 |
| `__memmove_avx512` (control) | 9.30 | 11075 | 4.80 | 5664 | 1.94 |

**Whole-run IPC** = instr_event/cycles_event = **3.423**  
(event counts: cycles=29123899411, instr=99687162966)

**Implied IPC (coherent with Zen4c)**:

- IPC(`keccak_p`) ≈ **4.78** (instr share **above** cycles share → compute-dense, high IPC)
- IPC(rest excl. keccak_p) ≈ **2.24**

No longer requires rest IPC>6.5.

### Callgrind Ir (scale=8, summary Ir=71,787,303)

`callgrind_annotate` keccak_p-related lines sum ≈ **67.2% Ir** (51.61+12.77+1.61+1.24).  
Same order as HW instructions **65.0%**; both **above** HW cycles **46.6%** (classic high-IPC hot function).

Evidence paths:

- `t1_keccak_recheck/cycles_spin12.symbol.txt` / `cycles_spin12.data`
- `t1_keccak_recheck/instructions_spin12.symbol.txt` / `instructions_spin12.data`
- `t1_keccak_recheck/callgrind.out` + `callgrind.annotate.txt`
- `t1_keccak_recheck/t1_summary.json`

## 4. Artifact cause (skid vs tooling)

| Hypothesis | Verdict |
|---|---|
| cycles skid attributes stalls to keccak | **Not supported**: under high sample count, instr% **exceeds** cycles% (65 vs 47); skid would inflate cycles relative to instr |
| Symbol/script merge artifact | **Confirmed** (section 2) |
| Undersample noise | **Confirmed** (35 → 119K samples; stable ~47% cycles) |

## 5. Mine or not?

**keccak is real #1 on Lane2 CREATE (~47% cycles / ~65% instr / ~67% Ir)**, but:

1. Lives in `keccak` / `alloy-primitives` soft backend — **outside mega-evm editable surface**.
2. High-IPC compute kernel → Ir judge **sees it** (higher share), not an Ir-blind class.
3. Necessary algorithmic work on CREATE address/initcode; mega-evm leverage is **fewer hashes / cache / dedupe**, not round-function micro-opts.

**Recommendation**: **Do not list as primary Ir-blind mine**. Treat as workload structure fact (CREATE lane keccak-dominated). True Ir-blind surfaces remain **HashMap/hashbrown, memmove, alloc** (div>1).

## 6. Self-cert

1. Cleanup: measurement finished; data under `t1_keccak_recheck/`
2. Credentials: no PAT this task
3. Identity: aro creds at push time
4. megaeth-labs: zero remote writes
