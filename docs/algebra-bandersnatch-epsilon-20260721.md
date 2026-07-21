# Algebra Bandersnatch Ir epsilon B-class decision — 2026-07-21

## Decision

| lane | worst measured A/A spread | 3× lower bound | selected epsilon |
|---|---:|---:|---:|
| `algebra-bandersnatch-field` | `0.000915658505%` | `0.002746975515%` | `0.003%` |
| `algebra-bandersnatch-msm` | `0.004935667336%` | `0.014807002009%` | `0.015%` |

Both selected values are simple decimal settings above the mandatory `3 × worst_AA_spread` lower bound. The field lane uses `0.003%`; MSM uses `0.015%`.

## Evidence and scope

- Environment fingerprint: `codspeed=4.18.3;cargo-codspeed=5.0.1;valgrind=3.26.0.codspeed5;rustc=1.96.0`
- Initial selfchecks: field `0.000133336691%`; MSM `0.000526912864%`.
- Required post-adjustment selfchecks: field `0.000101963375%`; MSM `0.000641442262%`; both PASS.
- The provisional calibration exposed higher row-level A/A: field `probe/v4/8` floor `0.001831317010%` implies raw A/A `0.000915658505%`; MSM `probe/v3/1` floor `0.009871334673%` implies raw A/A `0.004935667336%` (`floor = raw × 2`).
- The final decision uses the worst applicable production-profile value across selfchecks and calibration rows.
- Scope: target-spec-only B-class adjustment; no ARO judge code, arithmetic source, Salt spec, ADX/BMI2 policy, or production configuration changed.
- Symmetric consequence: the tighter Ir epsilon applies equally to optimization acceptance and regression rejection.
- Required continuation: re-run selfcheck after this final spec adjustment, then recalibrate all `original/v1..v4 × scale 1/8` probe rows before pipeline.
