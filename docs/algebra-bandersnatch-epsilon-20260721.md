# Algebra Bandersnatch Ir epsilon B-class decision — 2026-07-21

## Decision

| lane | worst measured A/A spread | 3× lower bound | selected epsilon |
|---|---:|---:|---:|
| `algebra-bandersnatch-field` | `0.000133336691%` | `0.000400010073%` | `0.0005%` |
| `algebra-bandersnatch-msm` | `0.000641442262%` | `0.001924326787%` | `0.002%` |

Both selected values are the next simple decimal settings above the mandatory `3 × worst_AA_spread` lower bound. The field lane uses `0.0005%`; MSM uses `0.002%`.

## Evidence and scope

- Environment fingerprint: `codspeed=4.18.3;cargo-codspeed=5.0.1;valgrind=3.26.0.codspeed5;rustc=1.96.0`
- Initial selfchecks: field `0.000133336691%`; MSM `0.000526912864%`.
- Required post-adjustment selfchecks: field `0.000101963375%`; MSM `0.000641442262%`; both PASS.
- The decision uses the worst observed production-profile value per lane across both selfchecks. Each selfcheck used two Ir A/A rounds.
- Scope: target-spec-only B-class adjustment; no ARO judge code, arithmetic source, Salt spec, ADX/BMI2 policy, or production configuration changed.
- Symmetric consequence: the tighter Ir epsilon applies equally to optimization acceptance and regression rejection.
- Required continuation: re-run selfcheck after the spec adjustment, then calibrate all `original/v1..v4 × scale 1/8` probe rows before pipeline.
