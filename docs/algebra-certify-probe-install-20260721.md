# Algebra certify preflight correction — 2026-07-21

## Failure

The first `algebra-bandersnatch-field` sweep naturally produced zero attempted and zero accepted candidates. Certify then failed its clean-baseline preflight because `correctness_oracle.build` requested `--example algebra_bandersnatch_field` before ARO had installed the ARO-owned example into the detached reverify worktree.

## Root cause

The onboarding shim `ark-algebra-aro-probes` intentionally commits only `src/lib.rs`; its manifest states that ARO installs benchmark and differential examples temporarily. `SpecTarget.bench` and `run_diff_probe` perform that installation at measurement/oracle time. A user-authored clean-baseline build command cannot require the absent temporary example before those stages run.

Only the two new Algebra specs used `--example` in `correctness_oracle.build`; existing targets do not use this incompatible pattern.

## B-class spec correction

Both Algebra targets now build the source-owned shim package without naming a temporary example:

`cargo build --release -p ark-algebra-aro-probes --features asm,std,parallel`

Benchmark and differential examples remain ARO-owned and are still compiled by their actual bench/differential gates. No `aro/*.py`, arithmetic source, Salt, ADX/BMI2 setting, or production configuration changed.

## Verification

A clean detached worktree at Algebra baseline `01b20e377460e7af9da069b0c96f2d1158a7b974` reproduced the absence of examples. The corrected source-owned package build passed in release mode with the required features. Both target selfchecks must be rerun before certify resumes.
