# B-class: mega-evm-v2 editable → limit packaging (triple gate)

**UTC**: 2026-07-24  
**Baseline**: `245476834741de1e1a615d22e6287621b64f30cb`  
**Evidence**: `docs/data/mega-evm-limit-editable-gates-20260724/`  
**Ruling input**: T1/T2/T3 accepted; expand editable to `limit/{limit,compute_gas,frame_limit}.rs` only after gates.

## Gate triple

| Gate | Result | Evidence |
|---|---|---|
| 1 Fingerprint | **PASS** | Enhanced DIFF ×2 identical: `DIFF 64ad855787b396d0` |
| 2 Call-trace / intersection | **PASS** | sweep_hotloop symbol hits limit/check_limit family (n=10/10/4 across cycle/instr/lane1); DIFF corpus has `run_compute_heavy` + `run_data_heavy` |
| 3 Mutation sensitivity | **PASS** | three mutations each RED + restore |

### Mutation matrix (enhanced DIFF = 4D LimitUsage + halt tag)

| Mutation | Kind | DIFF | Restore |
|---|---|---|---|
| miss_record | 漏记: skip `record_gas_used` in `record_compute_gas` | `df096eb7845c5171` RED | OK |
| wrong_order | 错序: invert **record-then-check** → check-then-record | `1b8ac55ab4e6405c` RED | OK |
| wrong_count | 错计数: double `record_gas_used` | `cd113ab6bb37bc78` RED | OK |

### Blind-spot closed

- **Stock DIFF** (success/gas/output/storage only): miss_record + wrong_count already RED; **dimension-priority reorder of `check_limit` was BLIND** (sticky per-site latch → multi-dim unlatched exceed never appears).
- **Protocol 错序** (record↔check inversion) is the observable 错序 red-line for packaging edits.
- Promoted ARO probe `probes/evm_semantics_diff.rs` marker `LIMIT_EDITABLE_ENHANCED_DIFF_V1` (sha256 `2b56654092e1956bd7f8cc299da093c730817153d3f97277556e2a6a619461b3`). Stock backup under evidence dir.

## Editable in force (post-gate)

```
crates/mega-evm/src/evm/host.rs
crates/mega-evm/src/evm/instructions.rs
crates/mega-evm/src/limit/limit.rs
crates/mega-evm/src/limit/compute_gas.rs
crates/mega-evm/src/limit/frame_limit.rs
```

Note: previous mega-evm-v2 used directory `crates/mega-evm/src` (already tree-wide). Post-gate policy is **intersection-tight** list for packaging mine (host+instructions+three limit files). Other `limit/*` trackers stay out until separate gates.

## Next

1. Re-run push1 packaging seed (`targets/mega-evm-v2-t2-push1-seed.json` + seeds a/b + limit-check jb).
2. Queue T3② host redundant map touch seeds (`t3-host-map-seeds-queued.json`).
3. Ship stops before PR.

## Self-cert

1. Mutations only in disposable worktrees; target tree clean of mutations  
2. Enhanced DIFF is ARO-side only  
3. megaeth-labs: zero remote writes this gate  
4. Credentials: none  
