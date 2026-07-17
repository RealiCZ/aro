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

### Dual-green rule (supplementary tests at ship time)

Every new test added when packaging a PR must pass on **both**:

1. the **baseline** worktree (clean checkout at the campaign `baseline_ref` /
   stamp `baseline_sha`), and
2. the **PR branch** (baseline + certified edits + the new tests).

A test that is green on both pins *preserved* behavior. A test that only passes
on the PR branch can mask a real behavior change; a test that only passes on
baseline means the certified edit broke something the test caught — either case
is a signal to **rewrite the test**, not to tweak src bytes. **Never modify
certified/src bytes to make a ship-time test pass.**

### Post-certification commits (only two kinds)

After terminal certification, the **only** commits allowed on the PR branch
before `aro ship conformance` / `gh pr create` are:

| kind | commit message shape | rules |
|---|---|---|
| Supplementary tests | `test(<crate>): cover <fn>` | dual-green (above); real assertions; no src changes |
| Mechanical formatting | `style: cargo fmt` | run `cargo fmt --all` **twice**; second run must produce **no diff** (idempotency). One commit. No hand-edited style drift mixed into perf commits. |

Anything else (drive-by refactors, "fix" src edits, silent rebases of certified
hunks) is out of bounds — re-certify instead.

## 3. Multi-lane merge gate (before opening)

A single campaign's verdict is one workload's opinion. Before recommending any
merge, read permtree ledgers (`memory/permtree/*.jsonl`) for `MERGE GATE` lines (or the
`conflicts` list in `union-report.json`): a function accepted in one lane but
regressed/rejected in another is a CONTRADICTION.

- A conflict on a function your PR touches: either resolve it (re-measure on the
  contradicting workload) or disclose it verbatim in the PR body — the reviewer
  decides with both numbers on the table. Silently shipping the winning lane's
  number is a protocol violation.
- No ledger for the target yet (first campaign): say so in one line and move on.

## 4. Numbers carry their origin

Quote the manifest's delta verbatim (metric and sign convention included), or,
if you re-measured, state where and how. Never blend the campaign's gates into
invented statistics ("differential at 95% CI" mixes two unrelated things and
reads as noise to a reviewer).

## 5. Body content

- What changed and why, in the target repo's engineering voice and language
  (English unless the repo says otherwise; never paste non-English text).
- Provenance: an autonomous optimization campaign; judge + critic details
  available; regime and critic verdict stated honestly.
- Every risk a reviewer needs to reject the PR in good conscience, including
  justified mutation survivors and any release-vs-debug guarantee differences.

## 6. Process rails (violations, not judgment calls)

- Never merge anything yourself; PRs are for the target repo's human review.
- Never force-push; never edit anything beyond the optimization diffs + their
  tests + a mechanical `style: cargo fmt` commit (see dual-green / post-cert
  table above).
- Opening a PR without the section-2 test evidence is a protocol violation.
- Opening a PR without a green `aro ship conformance` record bound to the
  branch `head_sha` is a protocol violation (`run-to-pr.md` §3).
- Opening overlapping PRs without declaring the relationship is a protocol violation.
- Follow the target repo's PR conventions (labels, title format, templates); a
  failing convention check (label-check etc.) is yours to fix, not to ignore.
