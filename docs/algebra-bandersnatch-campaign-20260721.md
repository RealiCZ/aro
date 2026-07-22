# Algebra Bandersnatch ARO campaign report — 2026-07-21

## Final decision

Both production-aligned lanes completed onboarding, selfcheck, final row calibration, first sweep, and certify. The campaign stops naturally before ship with zero accepted and zero mergeable edits. No package worktree, ship branch, push of candidate code, or PR was created.

Baseline: Algebra `01b20e377460e7af9da069b0c96f2d1158a7b974`. Environment fingerprint: `codspeed=4.18.3;cargo-codspeed=5.0.1;valgrind=3.26.0.codspeed5;rustc=1.96.0`.

## Lane gates

| lane | final epsilon | calibrated rows | sweep | certify |
|---|---:|---:|---|---|
| `algebra-bandersnatch-field` | `0.003%` | 10/10, all `0.003%` | no actionable frontier; 0 attempted, 0 accepted | preflight PASS; exit 2, zero survivors |
| `algebra-bandersnatch-msm` | `0.015%` | 10/10, all `0.015%` | 2 attempted, 0 accepted; 1 unlocated | preflight PASS; exit 2, zero survivors |

The epsilon choices satisfy `selected >= 3 × worst applicable A/A`, including row-level calibration evidence. Field worst raw A/A was `0.000915658505%` (3× `0.002746975515%`); MSM worst was `0.004935667336%` (3× `0.014807002009%`).

## Sweep details

### Field

The profile produced no actionable functions above the aligned editable/frontier policy. The run ended after 34.6 seconds with 0 attempted and 0 accepted. Evidence: `.aro-runs/algebra-bandersnatch-field-auto-20260721/{events.jsonl,manifest.json,reverify.json,decision-tree.html}`.

### MSM

| self-time | function | result |
|---:|---|---|
| `76.88%` | `mul_assign` | two agentic rounds, no usable `.rs` edit; `no-candidate` / diminishing returns |
| `10.31%` | `add_assign` | two agentic rounds, no usable `.rs` edit; `no-candidate` / diminishing returns |
| `3.18%` | `call_mut` | source unlocated; skipped |

The sweep ran for 717.1 seconds. Manifest: 0 accepted, 0 mergeable. Certify clean-baseline preflight passed and stopped with `recheck produced zero reverify-pass survivors — nothing to certify`. Evidence: `.aro-runs/algebra-bandersnatch-msm-auto-20260721/{events.jsonl,manifest.json,reverify.json,decision-tree.html,agent-transcripts/}` and `memory/permtree/algebra-bandersnatch-msm.jsonl`.

## Certify preflight correction

The first Field certify attempt exposed a target-spec integration defect: clean reverify worktrees do not yet contain ARO-owned temporary examples, but both specs had named those examples in `correctness_oracle.build`. The source-owned shim build was verified in a clean detached worktree, both specs were corrected to build the shim library, and both selfchecks/certify preflights then passed. No `aro/*.py` or arithmetic source changed. See `docs/algebra-certify-probe-install-20260721.md`.

## ADX/BMI2 decision support

A one-round, seven-sample, scale-8 manual comparison outside ARO found the following medians when enabling `+adx,+bmi2` relative to explicit disablement:

- Field mixed batch: `174629.812 ns → 165723.094 ns`, latency `-5.1003%`.
- 256-point MSM: `3880268.375 ns → 3154058.625 ns`, latency `-18.7155%`.

The Field direction is relatively stable. MSM is noisy and includes an `11.27 ms` enabled-run outlier. These are exploratory numbers only; no spec or production policy changed. See `docs/algebra-adx-bmi2-comparison-20260721.md`.

## Salt closeout cross-check

- `server/salt-final` is pushed at `6c065bcb642083ae2ddd457159868928d3374e51`.
- All six Salt lanes satisfy `0.02% >= 3 × worst A/A`, including the historical shared worst `0.0035%` (3× `0.0105%`).
- The `salt-witness` certify baseline timeout remains unfixed at the spec layer: `run.timeout` is still 1800 seconds and no larger test-specific override is configured. The prior empty-manifest timeout remains correctly classified as infrastructure limitation, not candidate failure.

## Natural stop

No remaining candidate requires adjudication. Further work requires a new decision: expand/isolate the Field frontier, seed a targeted MSM hypothesis, repeat the ADX/BMI2 experiment with counterbalanced production-build runs, or change the Salt witness timeout spec. None is started by this report.
