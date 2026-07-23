# Lane1 REX6 SSTORE/LOG gates @ 2454768

Date: 2026-07-23
Branch: server/mega-evm-rex6
Baseline: `245476834741de1e1a615d22e6287621b64f30cb`

## Gate 1 — deterministic fingerprint: PASS
- validator baseline pin updated; DIFF `6f26a41c0c58774723597fb0e1e58c07bb7e8bf5b3087b3f8aa293a10c00ec21` byte-identical ×2
- case_69 OK; cleanup verified
- evidence: `docs/data/mega-evm-rex6-lanes-20260722/sstore-log/validation.json`

## Gate 2 — call-trace/editable: PASS
- full-run gen `20260723T092836Z-full-2d55d704206a`
- timed Ir 19,606,719 / differential Ir 7,735,734
- proposed editable (9): host.rs, instructions.rs, external/gas.rs, limit/{compute_gas,data_size,frame_limit,kv_update,limit,state_growth}.rs
- current → that generation

## Gate 3 — mutation sensitivity: PASS
- semantic: gas.rs `sstore_set_gas_for_multiplier` +1 → DIFF changed to `3db86c29...`; restore recovered original
- perf: host.rs inspect_storage 50k black_box burn → DIFF unchanged; Ir 19,595,833 → 1,819,632,227
- evidence: `docs/data/mega-evm-rex6-lanes-20260722/sstore-log/mutation/`

## Spec
- `targets/mega-evm-rex6-sstore-log.json`
- icount_epsilon_pct **0.05** (worst A/A 0.01088%, 3×=0.0326%)
- terminal_lane=probe scales [1,8]; floors 10 rows (max 0.503% probe/v3/1)
- ship_target origin/cz/feat/rex6-preview; no megaeth-labs writes

## Next
pipeline first run; stop before package/open if candidates; else true-negative and continue lane sequence.

## Pipeline first run: TRUE NEGATIVE

- out: `.aro-runs/mega-evm-rex6-sstore-log-auto-20260723`
- attempts: sstore 5.5%, push8 4.0%, inspect_storage 2.6% — all no-candidate (agent dry)
- 0 accepted / 0 mergeable; exit 2 certify work order
- profile floor dominated by hash_bytes_long/keccak (runtime/crypto), not editable SSTORE path
- next: Lane 2 CREATE/CREATE2 under same three-gate discipline
