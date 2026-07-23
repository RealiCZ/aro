# mega-evm REX6 ARO rebattle record (re-settle)

Date: 2026-07-23
Status: re-settle + epsilon/floors + pipeline first run complete; stopped at certify work order (exit 2)
Class: B — operator decisions recorded; no megaeth-labs remote writes

## Sync and re-settle

- ARO origin/main already contained algebra PR #58 (5064e9b).
- Prior campaign on preview pin 996c16a completed 2026-07-22 (pipeline dry; lane proposal approved; Lane1 fingerprint gate landed).
- Preview moved: 996c16a..2454768 (3 commits: docs + state-test Rex6 map + small execution.rs note).
- One-shot re-settle: baseline_ref == ship_target head == `245476834741de1e1a615d22e6287621b64f30cb`.
- ship_target remains origin/cz/feat/rex6-preview.
- Intermediate clone materializes local cz/feat/rex6-preview; target checkout detached at pin.
- T51 tried/lessons preserved; unstamped historical lessons downgraded on fresh profile.
- Checkpoint push to RealiCZ/aro denied for mega-putin (403). Local commits on server/mega-evm-rex6 + format-patch evidence under docs/data/mega-evm-rex6-rebattle-20260723/.

## Epsilon (whole-probe Ir)

- selfcheck @0.01: Ir 1728565858 / 1728522075, spread 0.002532941067657824%
- selfcheck --rows: spread 0.006792410633858276%
- selfcheck @0.03: Ir 1728520343 / 1728527349, spread 0.0004053169423269848%
- worst applicable: **0.006792410633858276%**
- 3x lower bound: **0.020377231901574828%**
- provisional 0.01% fell below bound → **revised to 0.03%** (margin ~0.0096 pp; still 3.3x tighter than pre-campaign 0.1%)
- tool fingerprint: codspeed=4.18.3;cargo-codspeed=5.0.1;valgrind=3.26.0.codspeed5;rustc=1.96.0

## Criterion floors (T48)

- four-round calibrate PASS on exact baseline `2454768`
- rows: **192**; min/clamp 0.01%; median ~0.0585%; max ~2.171% (mixed_workload/rex6)
- at clamp: 37; >0.1%: 71; >1.0%: 3
- elapsed ~233s; selfcheck --rows: live set matches all 192 keys

## Pipeline first run (re-settled baseline)

- out: .aro-runs/mega-evm-v2-auto-20260723
- bootstrap re-pin already current; preflight baseline==head, ahead=0
- fresh profile frontier (attempted):
  - push1 19.52% — instructions.rs — no-candidate (2 dry agent rounds)
  - return_result 1.82% — precompiles.rs — no-candidate
  - sload 1.54% — host.rs + instructions.rs — no-candidate
- runtime floor frames: hash_bytes_long 7.07%, get_mut 4.42%, rustc_entry 3.24%, hash 3.01%
- T51 downgraded unstamped historical lessons; no manual tried/lesson purge
- generator preflight PASS (codex); six agent rounds produced zero usable .rs edits
- frontier dry streak=3; factory mode off → attempt_abort
- manifest: **0 accepted / 0 mergeable**
- certify STOP work order (zero reverify-pass survivors)
- pipeline exit **2** (designed); package/conformance/open not run
- elapsed ~1096s; max RSS ~1.58 GiB
- no candidate branch, no megaeth-labs push/PR/comment

## Lane coverage

Prior static audit (docs/mega-evm-rex6-lane-coverage-proposal-20260722.md) still applies on the new pin:
timed probe remains REX4-fixed; differential stops at REX5; REX6 surfaces uncovered.
Proposal status was already approved 2026-07-22 for sequential B-class lane build (stop on first candidate).
Lane1 fingerprint gate exists on ARO branch; call-trace WIP retained untracked under
docs/data/mega-evm-rex6-lanes-20260722/sstore-log/call-trace/.
After re-settle, Lane1 evidence bound to 996c16a must be revalidated on `2454768` before mutation/spec/pipeline of that lane.

## Natural stop / next

1. Owner push of server/mega-evm-rex6 (or supply RealiCZ write token) — local HEAD recoverable via format-patch.
2. Continue approved Lane1 call-trace → mutation → lane-local epsilon/floors → lane pipeline (stop on first candidate).
3. Do not resume main mega-evm-v2 pipeline past certify without candidates; zero-candidate dry streak is a true negative under current REX4 probe coverage of REX6 tree.
