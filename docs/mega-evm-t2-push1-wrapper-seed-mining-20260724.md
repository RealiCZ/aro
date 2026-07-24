# T2: push1 wrapper seed mining — full report

**UTC date**: 2026-07-24  
**Decompose**: `docs/mega-evm-t2a-push1-wrapper-decompose-20260724.md`  
**Pipeline run**: `.aro-runs/mega-evm-v2-t2-push1-seed-20260724/`  
**Evidence copy**: `docs/data/mega-evm-hwcounters-20260723/t2_push1_wrapper/pipeline/`

## 1. Decompose result (ceiling)

| Quantity | Value |
|---|---:|
| push1 HW cycles | **18.27%** |
| push1 Callgrind Ir inclusive | **20.08%** |
| Wrapper share of push1 (Callgrind limit/* floor) | **~27%** |
| Wrapper share of push1 (annotate upper band) | **~35–50%** |
| **Theoretical whole-program cycle ceiling if packaging→0** | **~5–9%** |

Structure: `gas_before` → `stack::push::<1>` body → `borrow_mut` → `record_compute_gas`/`check_limit`.

## 2. Seed-driven agentic result

| Item | Result |
|---|---|
| Spec | `targets/mega-evm-v2-t2-push1-seed.json` (notes + hot_path seed) |
| Seeds applied | `push1` (bias applied) |
| Seeds skipped | `check_limit`, `record_compute_gas`, `hash_bytes_long` (**not on frontier / not in editable scope**) |
| Attempts | push1 no-candidate; sload no-candidate; frame_init no-candidate |
| **Accepted** | **0** |
| Mergeable | 0 |
| Exit | frontier dry / exit 0 |

**Critical scope fact**: mega-evm-v2 editable set is:

```
['crates/mega-evm/src']
```

`AdditionalLimit::check_limit` / `record_compute_gas` live under `crates/mega-evm/src/limit/**`, which is **outside** this editable set. The packaging fast-path seed (direction a/b) **cannot be implemented** under current mega-evm-v2 constraints without expanding `constraints.editable` to include limit trackers.

What *is* editable: `wrap_op_compute_gas` shell in `instructions.rs` — agent made **no usable .rs edits** in 2 rounds (likely LLVM already owns thin shell; body is revm `stack::push`).

## 3. Wall-clock

No accepted candidate → **no counterbalanced wall-clock stage**.

## 4. Recommendations

1. **To mine packaging for real**: open a B-class target variant with  
   `editable` including at least:
   - `crates/mega-evm/src/limit/limit.rs`
   - `crates/mega-evm/src/limit/compute_gas.rs`
   - `crates/mega-evm/src/limit/frame_limit.rs`
   plus existing host/instructions; re-run seeds on `check_limit`/`record_compute_gas`.
2. Keep DIFF byte-identical + four-dimension record order red lines.
3. Do not expand coalesce-across-opcodes (direction c) without a dedicated semantic proof plan.
4. Ship still stops before PR.

## 5. Self-cert

1. Cleanup: sweep finished; no leftover agent required  
2. Credentials: none in this phase  
3. Identity: aro for hwcounters push  
4. megaeth-labs: zero remote writes; candidates none  
