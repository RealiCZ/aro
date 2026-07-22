# mega-evm REX6 ARO rebattle record

Date: 2026-07-22
Status: target refresh, epsilon, and per-row floor gates green; pipeline pending
Class: B — operator decisions are recorded here; no megaeth-labs remote writes are authorized

## Sync and target selection

- ARO `origin/main` synchronized to `5064e9b2141018f2e36dad083732c12e3b3946d9`.
- PR #58 is present on main as `feat(targets): algebra fourth-target campaign — specs, evidence, and Salt thread-gated conformance (#58)`.
- `server/algebra-target` was not modified.
- mega-evm intermediate `origin/main`: `3b550bd57a39b55e7664a41d8fdca889a5247bad`.
- REX6 preview ref: `origin/cz/feat/rex6-preview` at `996c16a91d071e3bb95780ea7dc5d4f1677bf746`.
- At refresh time the preview ref exactly matched the user-specified SHA and was 39 commits ahead of current main with no main-only commits.
- `targets/mega-evm-v2.json` now pins that full SHA and sets `ship_target` to `origin/cz/feat/rex6-preview`. Pipeline bootstrap therefore resolves and re-pins against the preview branch rather than `origin/main`.

## Preserved campaign state

T51 exact-target/freshness rules are active on ARO main. Existing `memory/permtree/mega-evm-v2.jsonl` and shared `memory/lessons.jsonl` are preserved unchanged. No old tried or lesson entry is manually deleted or duplicated. Stale entries may inform generation but cannot suppress a rewritten frontier unless target, baseline SHA, and freshness all match.

The target checkout contained one untracked historical probe and six registered nested campaign worktrees. No ARO, Cargo, CodSpeed, Valgrind, Codex, or reporter process was active. These residuals were inventoried and left untouched; no reset or deletion was needed to fetch the new refs.

## Frontier requirement

The new baseline must be profiled from scratch. Priority review includes the REX6 additions in the instruction dispatch, `evm/host.rs`, the limit trackers, and `external/gas.rs`. Historical tried state is not accepted as profile evidence for the new baseline.

## Pending B-class measurement decisions

1. Run selfcheck at the existing `0.1%` epsilon and retain raw A/A Ir values.
2. Select a provisional epsilon at or above three times the worst applicable A/A spread.
3. Re-run selfcheck after the epsilon change and recompute against both runs.
4. Calibrate per-row floors independently for the criterion bench lane. Criterion row drift does not back-propagate into the whole-probe epsilon; the lanes have different noise models and the floor file is the row-level mechanism.
5. Launch pipeline only after baseline, selfcheck, epsilon, and floors are mutually consistent and green.

## Epsilon decision

The initial selfcheck retained the old `0.1%` spec epsilon and measured:

- Ir A: `1,728,496,321`
- Ir A repeat: `1,728,517,151`
- marker spread: `0.001205086423%`
- result: PASS

Three times that spread is `0.003615259270%`. The selected provisional epsilon is `0.01%`: ten times tighter than the old setting, 2.766 times the required three-spread bound, and symmetric for improvement and regression detection.

After changing the spec to `0.01%`, selfcheck measured:

- Ir A: `1,728,448,208`
- Ir A repeat: `1,728,449,910`
- marker spread: `0.000098469781%`
- result: PASS

The worst applicable spread across both selfchecks remains `0.001205086423%`; its three-times lower bound remains `0.003615259270%`. The selected `0.01%` epsilon therefore has `0.006384740730` percentage points of margin before row-level calibration. Tool fingerprint for both runs: `codspeed=4.18.3;cargo-codspeed=5.0.1;valgrind=3.26.0.codspeed5;rustc=1.96.0`.

## Per-row floor calibration

Calibration used a clean detached checkout of the exact REX6 baseline and the criterion bench lane. An initial two-round run was retained as evidence, then replaced by the documented four-round calibration because this is the first 192-row floor set and per-process hasher seeding makes a two-sample maximum under-conservative.

Final four-round floor set:

- baseline: `996c16a91d071e3bb95780ea7dc5d4f1677bf746`
- rows: `192`
- minimum / clamp: `0.01%`
- median: `0.062755626628%`
- maximum: `1.731721243075%`
- rows at clamp: `31`
- rows above `0.1%`: `78`
- rows above `1.0%`: `3`
- slowest-noise row: `salt_dynamic_gas/revm_pinned/create_10`
- calibration result: PASS, exit `0`

The final four-round run changed 164 of 192 floors relative to the two-round sample and raised 135, validating the conservative four-round choice. These criterion floors remain separate from the whole-probe `0.01%` epsilon.

Post-calibration `selfcheck --rows` passed: the live row set exactly matched all 192 floor keys. Its whole-probe A/A was `1,728,505,659 / 1,728,508,820`, spread `0.000182874559%`; the worst whole-probe spread across all three checks remains the initial `0.001205086423%`.
