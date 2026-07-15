# ARO run → PR (open a PR directly from a run's wins)

Turn an ARO run's accepted edits into a pull request, autonomously. Self-contained:
follow this top-to-bottom. Prerequisite contract: [`run-data.md`](run-data.md).

**The one rule that makes this safe to automate:**
> **Only ever PR `mergeable:true` edits.** A PR is a *proposal* a human reviews and merges:
> NEVER auto-merge, and NEVER open a PR for a 🟡 `mergeable:false` edit (relaxed regime,
> critic `pass-risk`, or terminal gate not CONFIRMED). Those are real wins that still need a
> human call; route them to a person, don't ship them.

`mergeable:true` = the strongest evidence ARO produces (random-input differential proved the
output byte-identical **and** the critic passed clean **and**, when the target declares
`terminal_bench_targets`, the criterion row-level Ir terminal gate returned
`TERMINAL_CONFIRMED`). That's safe to *propose*. Everything below gates on it.

---

## 1. Decide what to PR

```sh
cd ~/workspace/aro
python3 -m aro manifest .aro-runs/<RUN> --spec targets/<spec>.json
# optional: already-ran terminal stamp
# python3 -m aro manifest .aro-runs/<RUN> --spec targets/<spec>.json --terminal terminal.json
```

From `manifest.json`:
- `baseline_ref`: the commit the patches are anchored to.
- `accepted[]`: for each, `mergeable`, `fn`, `files`, `delta_pct`, `metric`, `regime`,
  `critic_verdict`, `hypothesis`, `patch_path`, and (when terminal is configured)
  `terminal`, `bench_ir_rows`, `profile_fingerprint`, and tool-written `terminal_stamp`
  (`verdict` + `source` path + `sha256` of that terminal.json). Optional additive fields:
  `quarantine` (outlier tripwire reason) and `reverify` (from `aro recheck candidates --apply`).
- top-level `terminal` (when present): the whole-checkout stamp shared by the bundle
  (includes `terminal_stamp` when tool-written).

Split:
- **`mergeable:true`** → candidates for this PR.
- **`mergeable:false`** → do NOT PR. Collect them into a short "needs human review" note
  (fn · Δ · regime · critic · terminal · quarantine · reverify + `hypothesis`) and hand
  that to a person instead.

**Quarantine hard rule:** an entry with a `quarantine` field (outlier `|Δ|` tripwire;
default-on at 5% even when the target omits `outlier_quarantine_pct`) is always
`mergeable:false` and must **never** be packaged into a PR. Same for any
`reverify.verdict` other than `reverify-pass`. Route those to a human.

If **zero** `mergeable:true` → **do not open a PR.** Report the needs-review list and stop.

After a gate-hardening deploy (new differential / `test_full`), run
`python3 -m aro recheck candidates --spec targets/<spec>.json --out .aro-runs/<RUN>`
(optionally `--apply`) before trusting an old manifest; see `docs/OPERATIONS.md` §13.7.

> **Headline number rule (Ir-first):** the PR title/body speed claim is the criterion
> row-level Ir Δ from `bench_ir_rows` (same signal CodSpeed CI reports). Wall-clock
> `delta_pct` is optional informational only, and must cite the noise-floor caveat
> (~8.4% layout noise on mega-evm; see #335). **Every number on the PR comes from
> artifacts** — `terminal_stamp` (path + sha256) and per-row data quoted from the
> verified `terminal.json` / `manifest.json`. Narrative performance claims without an
> artifact reference are forbidden. Hand-edited `terminal` strings without a stamp
> do not count.

---

## 1b. Terminal criterion-Ir gate (required when the target configures it)

Probe-level Ir wins do not imply criterion bench wins. Before a PR, measure both
worktrees with the external reporter CLI (plan §4):

```sh
# baseline worktree = clean checkout at baseline_ref
# candidate worktree = same + mergeable patches applied in `order`
python3 -m aro terminal targets/<spec>.json \
  --baseline <baseline-worktree> \
  --candidate <candidate-worktree> \
  --out .aro-runs/<RUN>/terminal.json \
  --update-manifest .aro-runs/<RUN> \
  --record --fn <primary-fn>
```

The terminal gate is **noise-aware**: each side is measured median-of-N times
(`terminal_measure_rounds`, default 3), and per-row classification uses calibrated
floors from `memory/floors/<spec>.json` (or a 1.0% default before first calibration).
See `docs/OPERATIONS.md` §13 for `aro terminal --calibrate` and the row-noise scaling law.

Verdicts:
- `TERMINAL_CONFIRMED` — ≥1 criterion row improved, none regressed beyond its floor → continue.
- `TERMINAL_CONFIRMED_WITH_TRADE` — ≥1 subject improvement and every subject regression is
  in a **tradeable** row family (not listed in `protected_row_families`) with
  `Δ ≤ tradeable_regression_cap_pct`. Reachable **only** when the target declares the
  row-family policy fields. **WITH_TRADE PR body MUST list every traded regression
  verbatim** from terminal notes (`traded: <row> <Δ%> (cap Y%)`). Protected-family
  band rows (`band: …`) may appear in notes but do not block the verdict.
- `TERMINAL_UNTOUCHED` — every row |ΔIr| ≤ floor → **do not open a PR**. Record the lesson
  (probe-vs-bench divergence; the #326/#332 failure shape). Stop.
- `TERMINAL_REGRESSED` / `TERMINAL_MIXED` → **do not open a PR**. Operator decision.
  On MIXED multi-candidate bundles, run `aro ablate` to attribute per-entry marginals
  and propose a shippable sub-bundle (see `docs/OPERATIONS.md` §13.8).

Hard errors (not verdicts): `profile_fingerprint` mismatch (config drift) or row-set
mismatch (bench keys differ across sides). Fix the environment; never force a PR.

`--list` / `--dry-run` prints the terminal config without needing the measure binary.

After a CONFIRMED or WITH_TRADE run, re-read `manifest.json`: only entries with a
tool-written `terminal_stamp` whose verdict is mergeable
(`TERMINAL_CONFIRMED` or `TERMINAL_CONFIRMED_WITH_TRADE`) plus the legacy
byte-identical / critic-pass conditions are `mergeable:true`. A bare
`"terminal": "TERMINAL_CONFIRMED"` without a stamp is **not** mergeable.

**Seam choice:** the terminal gate is a standalone CLI step between "patches applied
on worktrees" and "open the PR". `aro manifest` remains pure event-join; it stamps
terminal fields when given `--terminal` / auto-loaded `terminal.json` / `--spec` that
declares `terminal_bench_targets`. Loaded terminal docs are integrity-checked (verdict
recomputed from rows). `aro terminal --update-manifest` is the write-back
path used by this protocol.

---

## 2. Apply the patches (exact, on the branch you'll PR into)

Work in a clean worktree of the **target repo** (`manifest.spec`'s repo). Branch off the
repo's **default branch** (`main`/`develop`): that's what the PR merges into.

For each `mergeable:true` edit, **in `order`**, apply its `patch_path` (format in
[`run-data.md`](run-data.md) §4: `path:` + `<<<<<<< SEARCH … ======= … >>>>>>> REPLACE`):

- The `SEARCH` text must appear **exactly once** in the file. Replace that one occurrence
  with `REPLACE`. (`base-*` ids are seeded baseline, never in a manifest: ignore.)
- **If `SEARCH` doesn't appear, or appears more than once → STOP that edit.** It means the
  code drifted since `baseline_ref`; the win must be re-derived on current HEAD, not forced.
  Report it as "baseline drift: needs re-run", don't fuzzy-match.

> The patches are anchored to `baseline_ref`. If the default branch has moved past it, an
> exact match on the current branch means the change still applies cleanly; a miss means it
> doesn't, and silently forcing it is how you ship a wrong diff.

(The candidate worktree used for the terminal gate in §1b is this same applied state.)

---

## 3. Verify before you open anything (non-negotiable)

Don't trust the manifest: re-prove it compiles and passes tests on YOUR branch:

```sh
cargo build --release -p <crate>
cargo test  --release -p <crate>
```
(crate = the package the edited files live in; `correctness_oracle` in the run's spec has the
exact commands.) **If build or test fails → abort, open no PR**, report the failure. A green
build+test is the floor for proposing the change to a human.

ARO already proved correctness + Ir (probe and/or criterion); this step just confirms the
patch lands cleanly on the branch you're targeting.

---

## 4. Test evidence (coverage + mutation): follow `pr-discipline.md`

Both gates (meaningful tests covering the changed lines, and a mutation pass over the
changed files with survivors killed or justified) are defined ONCE in
`references/pr-discipline.md` section 2, together with the number-provenance and
one-change-one-PR rules that apply to any PR built from a run. Two facts specific to
THIS path:

- The tests are part of the PR diff; they are NOT part of the ARO run and do not appear
  in its report. That's expected: add them here (a `test(<crate>): cover <fn>` commit
  beside the perf one).
- This is a separate post-optimization step; it never touches or conflicts with the
  frozen tests ARO judged against.

ARO's perf edits typically hoist a predicate and branch the tail: that NEW branch is
exactly what existing tests miss, so a `mergeable` PR usually needs a few tests added.

## 5. Open the PR

One PR bundling the run's `mergeable:true` wins (they share a baseline and compound). Branch
name e.g. `aro/perf-<spec>-<shortsha>`.

> **Language: write the PR title and body in English** (the repo's language). The
> `hypothesis` in the manifest is already English; report speed as `X% fewer instructions`
> (Ir) on the named criterion row(s). Do not paste any non-English text into the PR.

Match the repo's house style: **read a recent merged PR first** and follow its shape (e.g.
megaeth-labs/mega-evm uses `## Summary` + `## Test plan` + an automated-agent footer).
**Describe only what THIS PR does**: do NOT list the wins you left out, and don't editorialize
about ARO; just say what changed and how it was verified.

**Title:** `perf(<crate>): <what changed> (<X% fewer instructions on <row>>)`.
Headline `X` = the primary row's |Δ| from `bench_ir_rows` (most-negative Δ preferred).
Do **not** put wall-clock % in the title.

**Body** (fill from the manifest + your own build/test results; state nothing you can't back):

```md
## Summary

Behaviour-preserving optimization of <fn(s)> in `<crate>`.

- `<fn>` (`<file>`): <hypothesis, trimmed to a sentence or two>.
  **|bench_ir_rows[row]|% fewer instructions** on criterion row `<row>`
  (from verified `terminal.json` / `terminal_stamp.source=<path>`
  `sha256=<hex>`; `profile_fingerprint=<fp>`).
- … (one bullet per mergeable edit, biggest |Ir Δ| first)

Optional informational (not the claim): wall-clock probe Δ was `delta_pct` under
ARO's A/A floor — layout noise on this crate has been measured ~8.4% (#335); do
not treat wall-clock alone as evidence.

## Test plan

- `cargo build --release -p <crate>`: green.
- `cargo test --release -p <crate>`: green, same passing-test count as baseline.
- Added unit tests covering the changed branches; `cargo llvm-cov -p <crate>` shows the diff's lines covered.
- `cargo fmt --all --check` / `cargo clippy -p <crate> --all-features`: clean (if the repo gates on these).
- **No behaviour change**: a random-input differential proves baseline vs. patched output is
  bit-for-bit identical.
- **Instruction-count win (criterion rows)**: local terminal gate
  `TERMINAL_CONFIRMED` with `bench_ir_rows` = <copy from manifest>; CodSpeed CI
  must report the same direction on the same rows (see §6b).

---
*This PR was generated by an automated agent.*
```

Open it as a normal PR for human review. Do **not** enable auto-merge.

---

## 6. Safety rails (recap)

1. PR **only** `mergeable:true`. 🟡 → human, never auto-PR.
2. Exact SEARCH match or **stop** (no fuzzy apply).
3. **Build + test must pass** on your branch or **no PR**.
4. **Cover the changed lines** with meaningful tests so the patch-coverage CI passes: real
   assertions, never coverage-padding. Tests go in the PR diff, not ARO's report.
5. PR is a proposal: never auto-merge; keep the "generated by an automated agent" footer.
6. Every claim/number on the PR comes from artifacts (`manifest.json` /
   `terminal_stamp` path+sha256 / verified `terminal.json` rows / the spec), never from
   memory or free-form narrative. Do not invent performance numbers.

---

## 6b. Post-PR: CodSpeed cross-check (mandatory when terminal ran)

After the PR opens, wait for the CodSpeed check. Compare its per-row instruction-count
deltas against the local `manifest.terminal.bench_ir_rows` (or each entry's
`bench_ir_rows`):

- **Same direction on the claimed rows** → leave the PR for human review.
- **Mismatch** (local CONFIRMED but CI untouched/regressed, or different rows moved) →
  **close the PR**, append a lesson citing config drift (local measure vs CI profile /
  rustc / bench set), and flag `profile_fingerprint` for operator inspection. Do not
  re-open until the drift is explained.

This closes the loop that #326/#332 missed: local wall-clock / probe signal looked real;
CI instruction counts said zero product difference.

---

## 7. Worked example: `mega-evm-medium` (historical wall-clock shape)

`aro manifest .aro-runs/mega-evm-medium` → 4 accepted, **1 `mergeable:true`** under the
legacy (pre-terminal) rule:

- ✅ PR this one: `sload` · **4.48% faster** · byte-identical · `crates/mega-evm/src/evm/host.rs`
  · patch `a6/patches/agent-r0-0.txt` · baseline `070c810f…`.
- ❌ Do NOT PR (needs human): `sstore` 19.22% faster (relaxed/pass-risk), `inspect_storage`
  8.61% & 7.06% faster (relaxed/pass-risk). Bundle these into a review note for a person.

Under the Ir-first rule (targets with `terminal_bench_targets`, e.g. `mega-evm-v2`), that
same sload win would further need `aro terminal` → `TERMINAL_CONFIRMED` with a nonzero
`bench_ir_rows` entry before `mergeable:true`. A terminal `TERMINAL_UNTOUCHED` result is
exactly the #326 outcome: probe looked good, criterion rows did not move → **no PR**.

So: worktree of mega-evm off its default branch → apply `a6/patches/agent-r0-0.txt`'s
SEARCH/REPLACE on `host.rs` (exact, once) → terminal gate on baseline vs candidate →
`cargo build/test -p mega-evm` green. The edit adds a new `if is_oracle { … } else { … }`
tail branch in `sload`, so **cover both sides**: a test where an oracle address with
`MINI_REX` enabled comes back cold (`is_cold == true`), and one where a non-oracle address
passes through unchanged: `cargo llvm-cov` then shows the changed lines covered. → branch
`aro/perf-mega-evm-070c810f` → PR titled e.g.
`perf(mega-evm): hoist redundant SLOAD oracle predicate (N% fewer instructions on <row>)`,
body as §5 (Summary + Test plan only: nothing about the 3 left-out wins). One clean PR;
the 3 relaxed wins go to a human out-of-band, NOT mentioned in the PR.
