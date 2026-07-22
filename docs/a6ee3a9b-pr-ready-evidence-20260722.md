# a6ee3a9b backport: PR-ready evidence

Date: 2026-07-22

## Decision

The upstream optimization is safe to present as a one-line Algebra backport. Correctness conformance is green. The deterministic Ir judge is neutral in both required lanes, and the counterbalanced Salt consumer campaign does not show a reproducible wall-clock regression or improvement.

No MegaETH PR has been opened.

## Source and candidate

- Upstream source: `a6ee3a9b` (`remove useless modular operation (#982)`)
- Algebra baseline: `01b20e377460e7af9da069b0c96f2d1158a7b974`
- Product backport commit: `0fe47338d31c73e7d72b4a60b75951088485ca1a` in the candidate series
- Validated candidate tree: `03ee25353a9ed5655af0a5f8ba4e82982de1189e`
- Salt consumer: `19419f4d13e6c615b7a94cf3d2bf53d1052f723c`

The product diff changes only `r[i % N] = carry` to `r[i] = carry` in `MontConfig::square_in_place`. The surrounding loop bounds `i` to `0..N`, so the modulo is redundant.

The remaining candidate commits are validation-harness changes. They are not part of the proposed one-line product PR.

## Correctness checklist

- [x] Salt path patches resolved to the validated candidate for `ark-ff`, `ark-ec`, `ark-serialize`, `ark-poly`, and Bandersnatch.
- [x] Full deterministic conformance exited `0`.
- [x] 45 `test result: ok` summaries; zero failed summaries and zero error lines.
- [x] Ordinary Salt tests ran with `--test-threads=4`.
- [x] `test-bucket-resize` ran with `--test-threads=4`.
- [x] Filtered ignored random stress ran with `--test-threads=4`; all 100 iterations passed.
- [x] Both `--no-default-features` test variants ran with `--test-threads=4`.
- [x] RISC-V no-default-features check passed after installing the exact `nightly-2026-03-20` target `riscv64imac-unknown-none-elf`.
- [x] Full-chain wall time: `285.76s`; max RSS: `1,859,188 KiB`.
- [x] Static gate verifier found all 23 Salt-backed target conformance test commands and all quick/full script calls bounded to four libtest threads.

## B-class conformance variant

The four-thread bound is CI-equivalent environment alignment, not test reduction. Salt CI has equivalent four-core concurrency. Every package, feature, filter, ignored-test selection, assertion, and workload remains intact.

The bound applies to every Salt test call because attempt 1 also livelocked during ordinary `cargo test`, before the resize-feature command. This confirms the known `SHARED_COMMITTER: spin::Lazy<Arc<Committer>>` plus Rayon initialization race is not feature-specific. Repeating an unbounded command until it happens to pass is explicitly rejected as a gate.

Issue `megaeth-labs/salt#146` is open and has been accepted. Attempt 1 is archived only; no follow-up issue comment was posted.

Full rationale: `docs/a6ee3a9b-conformance-thread-gate-20260722.md`.

## Ir A/B judge

Both lanes used scale 1, `RAYON_NUM_THREADS=1`, the existing target profile fingerprints, CodSpeed `4.18.3`, cargo-codspeed `5.0.1`, and Valgrind `3.26.0.codspeed5`.

- Field, epsilon `0.003%`: baseline `114,747,092 Ir`; candidate `114,746,998 Ir`; delta `-0.0000819%`; verdict `neutral-ir`.
- MSM, epsilon `0.015%`: baseline `277,650,590 Ir`; candidate `277,644,983 Ir`; delta `-0.002019%`; verdict `neutral-ir`.

Both absolute deltas are below their pre-existing floors. The compiler already removes nearly all observable instruction-count effect at these lanes.

## Salt consumer wall-clock A/B

Configuration:

- Surfaces: state-update, witness, Salt MSM.
- Strict ABAB: every baseline sample immediately followed by candidate.
- Five measured rounds per mode and surface; three samples per mode per round.
- Fifteen adjacent A/B pairs per surface, plus one warm-up pair excluded from summaries.
- CPU affinity `0-15`; `RAYON_NUM_THREADS=16`; scale 1; Salt checked-in release profile.

Results (positive delta means candidate slower):

- State-update: round-median delta `+5.994%`, MAD `4.793%`; adjacent-pair median `-0.652%`, MAD `6.370%`; candidate faster in `9/15` adjacent pairs and `2/5` rounds.
- Witness: round-median delta `+0.00843%`, MAD `0.0833%`; adjacent-pair median `+0.1736%`, MAD `1.105%`; candidate faster in `6/15` pairs and `1/5` rounds.
- Salt MSM: round-median delta `+0.2185%`, MAD `0.3245%`; adjacent-pair median `+0.01133%`, MAD `0.5332%`; candidate faster in `7/15` pairs and `1/5` rounds.

Interpretation: witness and MSM are effectively neutral. State-update is noisy and its round-level and adjacent-pair estimates disagree in sign, so it does not establish a reproducible regression. Combined with neutral deterministic Ir, the evidence supports correctness and performance neutrality, not a measurable speedup claim.

## Evidence map

Raw and summarized artifacts are under `docs/data/a6ee3a9b-pr-ready-20260722/`:

- product backport diff and candidate series patch;
- full conformance log, timing, and exit code;
- static RED/GREEN gate logs;
- Ir summary;
- wall-clock raw events, summary, environment, path-patch reports, and binary hashes;
- SHA-256 manifest.

## Delivery boundary

- PR draft: `docs/a6ee3a9b-algebra-pr-draft-20260722.md`
- Salt pin steps: `docs/a6ee3a9b-salt-pin-bump-20260722.md`
- Delivery target: `server/algebra-target`
- Stop after evidence push; do not open a MegaETH PR.
