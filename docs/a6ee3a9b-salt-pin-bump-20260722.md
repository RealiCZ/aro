# Salt pin bump steps after the Algebra PR merges

Do not perform these steps until the Algebra PR has merged and its final MegaETH Algebra commit SHA is known.

## Inputs

- Current Salt Algebra rev: `80ca69c37f79d5d00750edc1602af81b5f456695`
- Replacement: `<ALGEBRA_MERGE_SHA>`
- Salt baseline used for evidence: `19419f4d13e6c615b7a94cf3d2bf53d1052f723c`

## Procedure

1. Create a clean Salt branch from the intended integration base.
2. In the root `Cargo.toml`, replace the current rev for these four dependencies:
   - `ark-ed-on-bls12-381-bandersnatch`
   - `ark-ff`
   - `ark-ec`
   - `ark-serialize`
3. In `banderwagon/Cargo.toml`, replace the same rev for these four dependencies:
   - `ark-ec`
   - `ark-ed-on-bls12-381-bandersnatch`
   - `ark-ff`
   - `ark-serialize`
4. Verify exactly eight manifest occurrences changed and no old rev remains.
5. Refresh `Cargo.lock` from the repository root with the checked-in toolchain. A direct command is:

   ```text
   cargo update -p ark-ff --precise <ALGEBRA_MERGE_SHA>
   ```

   If Cargo reports an ambiguous package selection, use the exact package identifier from `cargo tree -i ark-ff` and rerun the same update. Confirm all Algebra git-source packages resolve to the same new commit.
6. Run metadata and verify each required package has a path/git source resolving to `<ALGEBRA_MERGE_SHA>`; do not accept mixed Algebra revisions.
7. Run the deterministic Salt conformance set with the CI-equivalent thread bound:
   - ordinary `cargo test -- --test-threads=4`;
   - `test-bucket-resize` with `-- --test-threads=4`;
   - filtered random stress with `--test-threads=4 --ignored --nocapture`;
   - both no-default-features test variants with `-- --test-threads=4`;
   - RISC-V no-default-features check using `nightly-2026-03-20` and `riscv64imac-unknown-none-elf`.
8. Confirm the working diff contains only the intended eight manifest rev edits plus the lockfile refresh.
9. Commit and open the Salt pin PR only under a separate explicit authorization.

## Expected validation reference

The pre-merge path-patched candidate passed the full chain at validated candidate `03ee25353a9ed5655af0a5f8ba4e82982de1189e` with Salt `19419f4d13e6c615b7a94cf3d2bf53d1052f723c`. The post-merge pin run should reproduce that conformance outcome before integration.
