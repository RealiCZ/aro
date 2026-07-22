# mega-evm REX6 Five-Lane Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. Work on `dev-tko-node-1` in `/nvme2/mega-engineer/workspace/aro`, branch `server/mega-evm-rex6`. Use strict TDD and two-stage review for every lane.

**Goal:** Build and consume five REX6-specific ARO lanes in ranked order, each gated by deterministic fingerprint, runtime call-trace/editable intersection, and mutation sensitivity before its target spec is created.

**Architecture:** Each lane gets an aligned timed probe and differential probe using the same production `MegaEvm::transact(MegaTransaction)` tuple on baseline `996c16a91d071e3bb95780ea7dc5d4f1677bf746`. Stable fingerprints encode only canonical observable outputs. A spec is created only after the three pre-spec gates pass; it receives an independently measured `≥3×` probe A/A epsilon, four-round floors, row-set selfcheck, then one fresh pipeline run. Stop the entire sequence immediately if any lane yields an accepted candidate; never run package/open without user approval.

**Tech Stack:** Rust probe examples injected into mega-evm, Python ARO selftests/validation scripts, Valgrind/CodSpeed instruction counts, ARO selfcheck/terminal/pipeline.

---

## Global invariants

- Baseline and ship target stay paired: exact SHA `996c16a91d071e3bb95780ea7dc5d4f1677bf746` and `origin/cz/feat/rex6-preview`.
- Before every long measurement: refresh preview once, require `baseline_ref == ship-target head == target checkout HEAD`, quiet host, clean tracked target state, and push an ARO checkpoint.
- No `megaeth-labs` remote writes. Only `RealiCZ/aro:server/mega-evm-rex6` may receive artifacts.
- Keep `RAYON_NUM_THREADS=1`, pinned tools, and existing profile-fidelity policy.
- Preserve T51 tried/lessons. Do not clear historical state.
- `push1` at 18.08% is record-only; do not create a manual seed or architecture edit in this task.
- Each lane owns separate run/evidence directories under `.aro-runs/mega-evm-rex6-lanes-20260722/<lane>` and `docs/data/mega-evm-rex6-lanes-20260722/<lane>`.

## Per-lane vertical workflow

### Task A: RED contract tests

**Files:**
- Modify/create: `tests/selftest_rex6_lane_artifacts.py`
- Planned probes/spec listed in the lane section below.

1. Add a failing selftest asserting both probe files exist, use REX6 and the production transaction entry, expose the expected BENCH/DIFF prefixes, and declare identical workload identity constants.
2. Add a failing assertion that deterministic encoders avoid `Debug` and unordered map iteration.
3. Run the focused selftest and retain the expected RED output.

### Task B: Minimal aligned probes

1. Implement the smallest timed and differential probes that satisfy the lane tuple.
2. Re-run focused tests to GREEN.
3. Inject/build both examples against a detached baseline worktree.
4. Run the differential at least twice; require byte-identical full output and record the real fingerprint. Never invent or precompute the expected value.

### Task C: Call-trace/editable proof

1. Run profile/call tracing on both probes with the same tuple.
2. Record exact production call paths and source files reached by both.
3. Set the proposed editable list to the verified intersection only. Remove any file with one-sided or merely static reachability.
4. Keep trace logs and a machine-readable intersection manifest.

### Task D: Mutation sensitivity

1. In a disposable detached target worktree, apply one semantic mutation in the proposed editable intersection.
2. Require differential output to change or fail; restore and require the baseline fingerprint again.
3. Apply one performance-only perturbation that preserves semantics and require the timed/Ir lane to detect the constructed direction while differential remains unchanged.
4. Archive patches, outputs, and restoration hashes. Do not retain mutations.

### Task E: Create spec only after A–D pass

1. Add `targets/<lane>.json` with exact baseline/ship pairing, verified probe/oracle, verified editable intersection, pinned tools, and no target write step.
2. Run selfcheck first with a conservative bootstrap epsilon and retain two-run Ir values.
3. Set lane epsilon to at least `3×` the worst observed whole-probe A/A spread; use a simple rounded value with documented margin.
4. Re-run selfcheck; if worse, recompute from the worst run.
5. Run four-round terminal calibration and `selfcheck --rows`; commit floors and raw evidence.

### Task F: Pipeline and stop gate

1. Push the ARO checkpoint.
2. Run one fresh pipeline for this lane.
3. If accepted/mergeable candidates exist, stop the five-lane sequence before package/open and report for user ship approval.
4. If zero candidates, archive as a true negative and continue to the next lane.
5. Perform spec-compliance and code-quality review before marking the lane complete.

## Lane 1 — REX6 SSTORE/LOG state transition

**Files planned:**
- Create: `probes/mega_evm_rex6_sstore_log.rs`
- Create: `probes/mega_evm_rex6_sstore_log_diff.rs`
- Create after gates: `targets/mega-evm-rex6-sstore-log.json`
- Create after calibration: `memory/floors/mega-evm-rex6-sstore-log.json`

**Tuple:** Exact REX6 variants for zero→nonzero, nonzero→nonzero, reset-to-original, SLOAD, and fixed LOG0/LOG1/LOG2 lengths. Start from #330 workload semantics for `sstore_heavy/rex6/{sstore_100,sload_100,sstore_sload_100}`.

**Fingerprint:** halt/success, gas, output, ordered logs, address/slot-sorted final state, and sorted SALT bucket IDs where observable.

**Initial editable hypothesis:** `evm/instructions.rs`, `evm/host.rs`, `external/gas.rs`, and the reached `limit/` tracker files; narrow by dual traces.

## Lane 2 — REX6 CREATE/CREATE2 single-window metering

**Files planned:**
- `probes/mega_evm_rex6_create.rs`
- `probes/mega_evm_rex6_create_diff.rs`
- `targets/mega-evm-rex6-create.json`
- `memory/floors/mega-evm-rex6-create.json`

**Tuple:** #330 `create_deploy/rex6/{create_10,create2_10}` semantics with fresh DB/EVM, fixed nonce/salt/initcode, net-new and pre-funded-no-code address variants.

**Fingerprint:** halt/success, gas, created addresses in order, caller nonce, created account nonce/balance/code hash/canonical bytes, sorted final state.

## Lane 3 — REX6 SELFDESTRUCT beneficiary accounting

**Files planned:**
- `probes/mega_evm_rex6_selfdestruct.rs`
- `probes/mega_evm_rex6_selfdestruct_diff.rs`
- `targets/mega-evm-rex6-selfdestruct.json`
- `memory/floors/mega-evm-rex6-selfdestruct.json`

**Tuple:** funded source to empty distinct beneficiary and funded source to existing distinct beneficiary, both through the production VM entry.

**Fingerprint:** halt/success, gas, source/beneficiary existence, balances, nonces, code hashes, observable destruction state, sorted storage.

## Lane 4 — REX6 applied EIP-7702 authority

**Files planned:**
- `probes/mega_evm_rex6_eip7702.rs`
- `probes/mega_evm_rex6_eip7702_diff.rs`
- `targets/mega-evm-rex6-eip7702.json`
- `memory/floors/mega-evm-rex6-eip7702.json`

**Tuple:** fixed applied-net-new, applied-existing, chain-ID mismatch, nonce mismatch, and rejected-code authorization-list variants derived from #330 helpers.

**Fingerprint:** ordered input classification through observable authority nonce/delegation code/balance, caller nonce, halt/error, gas, address-sorted authority state.

## Lane 5 — REX6 system-origin exemption / unscaled SALT

**Files planned:**
- `probes/mega_evm_rex6_system_exempt.rs`
- `probes/mega_evm_rex6_system_exempt_diff.rs`
- `targets/mega-evm-rex6-system-exempt.json`
- `memory/floors/mega-evm-rex6-system-exempt.json`

**Tuple:** real system-originated transaction plus ordinary-caller control for zero→nonzero SSTORE, value CALL to empty account, CREATE, and oracle/beneficiary volatile access under minimum-capacity and crowded SALT environments.

**Fingerprint:** full execution/state outcome; system-exempt capacity environments must match, ordinary caller must retain capacity sensitivity, and bucket IDs must be sorted.

## Final integration review

After all five true negatives—or immediately after the first candidate stop—run repository tests and static checks, verify all SHA-256 evidence manifests, confirm ARO local/remote equality, clean tracked worktrees, no credential residue, no active measurement processes, and zero megaeth-labs writes. Update `docs/mega-evm-rex6-rebattle-20260722.md` with the per-lane ledger and natural-stop reason.
