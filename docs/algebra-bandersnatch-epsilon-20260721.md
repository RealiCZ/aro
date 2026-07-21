# Algebra Bandersnatch Ir epsilon B-class decision — 2026-07-21

## Decision

| lane | worst measured A/A spread | 3× lower bound | selected epsilon |
|---|---:|---:|---:|
| `algebra-bandersnatch-field` | `0.000133336691%` | `0.000400010073%` | `0.0005%` |
| `algebra-bandersnatch-msm` | `0.000526912864%` | `0.001580738593%` | `0.002%` |

Both selected values are the next simple decimal settings above the mandatory `3 × worst_AA_spread` lower bound. The field lane uses `0.0005%`; MSM uses `0.002%`.

## Evidence and scope

- Environment fingerprint: `codspeed=4.18.3;cargo-codspeed=5.0.1;valgrind=3.26.0.codspeed5;rustc=1.96.0`
- Measurements: each lane selfcheck used two production-profile Ir A/A rounds.
- Scope: target-spec-only B-class adjustment; no ARO judge code, arithmetic source, Salt spec, ADX/BMI2 policy, or production configuration changed.
- Symmetric consequence: the tighter Ir epsilon applies equally to optimization acceptance and regression rejection.
- Required continuation: re-run selfcheck after the spec adjustment, then calibrate all `original/v1..v4 × scale 1/8` probe rows before pipeline.
