# PR discipline: rules for ANY pull request opened from a run

Both PR-opening paths point here: the mechanical `mergeable:true` path
(`run-to-pr.md`) and the delegated-review path for non-mergeable accepts
(`evaluate-run.md`). If a rule applies to "a PR built from ARO output", it lives
HERE and nowhere else; the two path docs keep only their own flow logic.

## 1. Decide first, act second

Form ALL verdicts (which edits ship, how they compose, how they group) before
opening ANY PR. Then open the whole set. Streaming side effects while still
reviewing is how overlapping PRs happen: a composition discovered at edit 4
cannot retroactively fix a PR opened at edit 2.

- Invariant: a given change appears in EXACTLY ONE open PR.
- Composed sequential edits are presented as the SQUASHED final state, not the
  archaeology (an intermediate the final state deletes gets no PR of its own).
- Deliberately offering a conservative subset and a full version as alternatives
  is allowed ONLY if both bodies name the relationship and which is the fallback.

## 2. Test evidence (both gates, before opening)

- **Coverage**: the lines your diff changes must be exercised by MEANINGFUL tests
  (real assertions, never coverage-padding); find gaps with the target repo's
  coverage tooling (e.g. `cargo llvm-cov --release -p <crate> --lib`). The edit
  is behavior-preserving, so a correct test passes on both old and new code.
  Tests ship in the same PR, in the repo's test style.
- **Mutation**: run the repo's mutation tooling scoped to the changed files
  (generically `cargo mutants -p <crate> --file <changed-file>`). Every surviving
  mutant in the changed region is either killed by a new assertion or justified
  explicitly in the PR body (equivalent mutant, or outside the observable
  contract).
- An invariant pinned only by a `debug_assert!` MUST gain a real test asserting
  its observable consequence: the assert is compiled out of release builds, so
  without a test the invariant is unguarded exactly where it matters.
- A changed line genuinely unreachable under invariants is not fake-covered:
  find a real input, or leave it uncovered and flag it for review.

## 3. Numbers carry their origin

Quote the manifest's delta verbatim (metric and sign convention included), or,
if you re-measured, state where and how. Never blend the campaign's gates into
invented statistics ("differential at 95% CI" mixes two unrelated things and
reads as noise to a reviewer).

## 4. Body content

- What changed and why, in the target repo's engineering voice and language
  (English unless the repo says otherwise; never paste non-English text).
- Provenance: an autonomous optimization campaign; judge + critic details
  available; regime and critic verdict stated honestly.
- Every risk a reviewer needs to reject the PR in good conscience, including
  justified mutation survivors and any release-vs-debug guarantee differences.

## 5. Process rails (violations, not judgment calls)

- Never merge anything yourself; PRs are for the target repo's human review.
- Never force-push; never edit anything beyond the optimization diffs + their tests.
- Opening a PR without the section-2 test evidence is a protocol violation.
- Opening overlapping PRs without declaring the relationship is a protocol violation.
- Follow the target repo's PR conventions (labels, title format, templates); a
  failing convention check (label-check etc.) is yours to fix, not to ignore.
