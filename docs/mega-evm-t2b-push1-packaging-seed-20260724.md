# T2b: push1 packaging seed after limit editable gate

**UTC**: 2026-07-24  
**Gates**: `docs/mega-evm-limit-editable-triple-gate-20260724.md` (PASS)  
**Run**: `.aro-runs/mega-evm-v2-t2b-push1-packaging-20260724/`  
**Spec**: `targets/mega-evm-v2-t2-push1-seed.json`  
**Baseline**: `2454768`

## Editable (post-gate)

host.rs · instructions.rs · limit/{limit,compute_gas,frame_limit}.rs

## Seeds

| Seed | Result |
|---|---|
| `check_limit` | **applied** (on frontier, 1.63% self) → attempt → **no-candidate** (2 dry rounds, agent no usable .rs edits) |
| `push1` | **applied** (19.29%) → **no-candidate** |
| `record_compute_gas` | **seed_skipped** (not on frontier name set) |
| `AdditionalLimit::check_limit` | **seed_skipped** (qualified name not on frontier) |

Frontier also walked `return_result` → no-candidate. Abort: frontier dry after 3 attempts, generator healthy.

## Verdict

| Item | Value |
|---|---|
| Accepted | **0** |
| Mergeable | **0** |
| Class | **True negative** (generator healthy; packaging surface opened; no agentic edit cleared gates) |
| Wall-clock | skipped (no accepted) |

## Interpretation

1. Opening `limit/**` in editable **worked**: `check_limit` is no longer out-of-scope; attempt files included `limit/limit.rs` + compute/frame trackers.
2. Agentic still produced **zero usable patches** on `check_limit` / `push1` under byte-identical DIFF (enhanced 4D+halt). Likely already tight LLVM / high semantic risk for manual-looking micro-opts.
3. `record_compute_gas` remains off the demangled frontier label set — profile attribution may fold into `check_limit` / inlined sites; seed name must match frontier `name` exactly.
4. Theoretical ceiling from T2a (~5–9% WP if packaging→0) is **not contradicted**; this TN says agentic mining did not find a safe win in 2 rounds/fn.

## Queued next

T3② host redundant map-touch seeds: `docs/data/mega-evm-limit-editable-gates-20260724/t3-host-map-seeds-queued.json`  
(`inspect_storage` / `sload` bias; no hashbrown/revm_state edits).

## Ship

**Stop before PR.** No candidates.

## Self-cert

1. Sweep finished; host quiet for this job  
2. No megaeth-labs writes  
3. ARO commit pending for this report  
4. Push ARO branch when owner PAT available  
