# Algebra Bandersnatch ADX/BMI2 manual comparison — 2026-07-21

## Scope

Exploratory B-class comparison outside ARO adjudication, using Algebra baseline `01b20e377460e7af9da069b0c96f2d1158a7b974` and the existing field/MSM probes. No target spec, arithmetic source, Salt configuration, or production build policy was changed.

Host: AMD EPYC 9754; hardware advertises both `adx` and `bmi2`. Each mode was compiled into a separate Cargo target directory. Required baseline rustflag `--check-cfg=cfg(coverage_nightly)` was preserved while toggling:

- off: `-C target-feature=-adx,-bmi2`
- on: `-C target-feature=+adx,+bmi2`

Both probes used `ARO_BENCH_SCALE=8`, one run per mode, seven samples per run, production wall-clock parallelism (`RAYON_NUM_THREADS` unset). Execution order was off then on.

## Results

| probe | off median | on median | on vs off latency | median speedup |
|---|---:|---:|---:|---:|
| field mixed Fq/Fr batch | `174629.812 ns` | `165723.094 ns` | `-5.1003%` | `+5.3745%` |
| 256-point MSM | `3880268.375 ns` | `3154058.625 ns` | `-18.7155%` | `+23.0246%` |

### Field raw samples (ns per mixed Fq/Fr batch)

- off: `177873.812, 174629.812, 176261.969, 174040.750, 174525.750, 175035.125, 174004.219`
- on: `171803.250, 173502.594, 169481.438, 165538.688, 165723.094, 165379.344, 164701.531`
- median absolute deviation: off `589.062 ns`; on `1021.563 ns`

### MSM raw samples (ns per call)

- off: `5700151.375, 5380116.875, 3647527.000, 5214517.875, 3880268.375, 3259653.875, 2756832.000`
- on: `11270444.125, 3972649.000, 2850003.875, 3138121.500, 3154058.625, 2968052.625, 4531025.875`
- median absolute deviation: off `1123436.375 ns`; on `304054.750 ns`

## Interpretation boundary

The field result is directionally positive and relatively tight. The MSM result is also directionally positive by median but has substantial within-run variation and one `11.27 ms` outlier in the enabled run. Because this was one non-counterbalanced run per setting, these values are decision-support evidence only, not an ARO acceptance result or sufficient basis for a production policy change. A production decision should use counterbalanced repeats and the actual Salt build/CI environment.
