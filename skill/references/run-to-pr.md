# ARO run → PR (open a PR directly from a run's wins)

Turn an ARO run's accepted edits into a pull request, autonomously. Self-contained:
follow this top-to-bottom. Prerequisite contract: [`run-data.md`](run-data.md).

**The one rule that makes this safe to automate:**
> **Only ever PR `mergeable:true` edits.** A PR is a *proposal* a human reviews and merges —
> NEVER auto-merge, and NEVER open a PR for a 🟡 `mergeable:false` edit (relaxed regime or
> critic `pass-risk`). Those are real wins that still need a human call; route them to a
> person, don't ship them.

`mergeable:true` = the strongest evidence ARO produces (random-input differential proved the
output byte-identical **and** the critic passed clean). That's safe to *propose*. Everything
below gates on it.

---

## 1. Decide what to PR

```sh
cd ~/workspace/aro
python3 -m aro manifest .aro-runs/<RUN>     # → manifest.json
```

From `manifest.json`:
- `baseline_ref` — the commit the patches are anchored to.
- `accepted[]` — for each, `mergeable`, `fn`, `files`, `delta_pct`, `metric`, `regime`,
  `critic_verdict`, `hypothesis`, `patch_path`.

Split:
- **`mergeable:true`** → candidates for this PR.
- **`mergeable:false`** → do NOT PR. Collect them into a short "needs human review" note
  (fn · Δ · regime · critic + `hypothesis`) and hand that to a person instead.

If **zero** `mergeable:true` → **do not open a PR.** Report the needs-review list and stop.

---

## 2. Apply the patches (exact, on the branch you'll PR into)

Work in a clean worktree of the **target repo** (`manifest.spec`'s repo). Branch off the
repo's **default branch** (`main`/`develop`) — that's what the PR merges into.

For each `mergeable:true` edit, **in `order`**, apply its `patch_path` (format in
[`run-data.md`](run-data.md) §4 — `path:` + `<<<<<<< SEARCH … ======= … >>>>>>> REPLACE`):

- The `SEARCH` text must appear **exactly once** in the file. Replace that one occurrence
  with `REPLACE`. (`base-*` ids are seeded baseline, never in a manifest — ignore.)
- **If `SEARCH` doesn't appear, or appears more than once → STOP that edit.** It means the
  code drifted since `baseline_ref`; the win must be re-derived on current HEAD, not forced.
  Report it as "baseline drift — needs re-run", don't fuzzy-match.

> The patches are anchored to `baseline_ref`. If the default branch has moved past it, an
> exact match on the current branch means the change still applies cleanly; a miss means it
> doesn't, and silently forcing it is how you ship a wrong diff.

---

## 3. Verify before you open anything (non-negotiable)

Don't trust the manifest — re-prove it compiles and passes tests on YOUR branch:

```sh
cargo build --release -p <crate>
cargo test  --release -p <crate>
```
(crate = the package the edited files live in; `correctness_oracle` in the run's spec has the
exact commands.) **If build or test fails → abort, open no PR**, report the failure. A green
build+test is the floor for proposing the change to a human.

ARO already proved speed + byte-identical equivalence; this step just confirms the patch
lands cleanly on the branch you're targeting.

---

## 4. Add tests so the changed lines are covered (the coverage CI)

The repo gates PRs on **patch coverage** (Codecov, via `cargo-llvm-cov`): the lines your diff
changes must be exercised by tests. ARO's perf edits typically hoist a predicate and **branch
the tail** (e.g. sload's `if is_oracle { … } else { … }`) — that NEW branch is exactly what
existing tests miss, so a `mergeable` PR usually needs a few tests added. **These tests are
part of the PR diff; they are NOT part of the ARO run and do not appear in its report — that's
expected, add them here.**

1. Find the uncovered changed lines:
   ```sh
   cargo llvm-cov --release -p <crate> --lib --html   # or the repo's coverage command
   ```
   Look at the edited file — the gaps are the new branch(es) the optimization introduced.
2. Write **meaningful** unit tests exercising BOTH sides of each new branch and **asserting the
   real behaviour** — not no-op calls for coverage. The edit is byte-identical, so a correct
   test passes against both old and new code; that's the point. Follow the repo's test style
   (e.g. inline `#[cfg(test)]` modules, or `tests/`).
3. Re-run coverage; iterate until the changed lines are covered and the patch gate clears.
4. Commit the tests in the SAME PR (a `test(<crate>): cover <fn>` commit beside the perf one).

Guardrails:
- **Real assertions, not coverage-padding.** A test that calls the function but asserts nothing
  is the coverage analog of a reward-hack — don't.
- This is a SEPARATE post-optimization step (the PR agent adds tests); it never touches or
  conflicts with the frozen tests ARO judged against.
- If a changed line is genuinely unreachable given invariants (e.g. a `debug_assert!` ARO
  added), don't fake-cover it — find a real input, or leave it and flag it for review.

## 5. Open the PR

One PR bundling the run's `mergeable:true` wins (they share a baseline and compound). Branch
name e.g. `aro/perf-<spec>-<shortsha>`.

> **Language: write the PR title and body in English** — the repo's language. The
> `hypothesis` in the manifest is already English; report speed as `X% faster`. Do not paste
> any non-English text into the PR.

Match the repo's house style — **read a recent merged PR first** and follow its shape (e.g.
megaeth-labs/mega-evm uses `## Summary` + `## Test plan` + an automated-agent footer).
**Describe only what THIS PR does** — do NOT list the wins you left out, and don't editorialize
about ARO; just say what changed and how it was verified.

**Title:** `perf(<crate>): <what changed> (<X% faster>)` — describe the change, not the tool.

**Body** (fill from the manifest + your own build/test results; state nothing you can't back):

```md
## Summary

Behaviour-preserving optimization of <fn(s)> in `<crate>`.

- `<fn>` (`<file>`) — <hypothesis, trimmed to a sentence or two>. **|delta_pct|% faster** on `<metric>`.
- … (one bullet per mergeable edit, biggest |Δ| first)

## Test plan

- `cargo build --release -p <crate>` — green.
- `cargo test --release -p <crate>` — green, same passing-test count as baseline.
- Added unit tests covering the changed branches; `cargo llvm-cov -p <crate>` shows the diff's lines covered.
- `cargo fmt --all --check` / `cargo clippy -p <crate> --all-features` — clean (if the repo gates on these).
- **No behaviour change**: a random-input differential proves baseline vs. patched output is
  bit-for-bit identical.
- **Speedup is real, not noise**: A/A noise floor + paired A/B + bootstrap CI cleared.

---
*This PR was generated by an automated agent.*
```

Open it as a normal PR for human review. Do **not** enable auto-merge.

---

## 6. Safety rails (recap)

1. PR **only** `mergeable:true`. 🟡 → human, never auto-PR.
2. Exact SEARCH match or **stop** (no fuzzy apply).
3. **Build + test must pass** on your branch or **no PR**.
4. **Cover the changed lines** with meaningful tests so the patch-coverage CI passes — real
   assertions, never coverage-padding. Tests go in the PR diff, not ARO's report.
5. PR is a proposal — never auto-merge; keep the "generated by an automated agent" footer.
6. Every claim/number on the PR comes from `manifest.json` / the spec — never from memory.

---

## 7. Worked example — `mega-evm-medium`

`aro manifest .aro-runs/mega-evm-medium` → 4 accepted, **1 `mergeable:true`**:

- ✅ PR this one: `sload` · **4.48% faster** · byte-identical · `crates/mega-evm/src/evm/host.rs`
  · patch `a6/patches/agent-r0-0.txt` · baseline `070c810f…`.
- ❌ Do NOT PR (needs human): `sstore` 19.22% faster (relaxed/pass-risk), `inspect_storage`
  8.61% & 7.06% faster (relaxed/pass-risk) — bundle these into a review note for a person.

So: worktree of mega-evm off its default branch → apply `a6/patches/agent-r0-0.txt`'s
SEARCH/REPLACE on `host.rs` (exact, once) → `cargo build/test -p mega-evm` green. The edit
adds a new `if is_oracle { … } else { … }` tail branch in `sload`, so **cover both sides**:
a test where an oracle address with `MINI_REX` enabled comes back cold (`is_cold == true`),
and one where a non-oracle address passes through unchanged — `cargo llvm-cov` then shows the
changed lines covered (clears the patch-coverage gate that left #326 at 77.7%). → branch
`aro/perf-mega-evm-070c810f` → PR titled e.g.
`perf(mega-evm): hoist redundant SLOAD oracle predicate (4.48% faster)`, body as §5
(Summary + Test plan only — nothing about the 3 left-out wins). One clean PR; the 3 relaxed
wins go to a human out-of-band, NOT mentioned in the PR.
