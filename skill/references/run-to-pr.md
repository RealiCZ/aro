# ARO run → PR (open a PR directly from a run's wins)

Turn an ARO run's accepted edits into a pull request, autonomously. Self-contained:
follow this top-to-bottom. Prerequisite contract: [`run-data.md`](run-data.md).

**The one rule that makes this safe to automate:**
> **Only ever PR `mergeable:true` edits.** A PR is a *proposal* a human reviews and merges:
> NEVER auto-merge, and NEVER open a PR for a 🟡 `mergeable:false` edit (relaxed regime
> without a reverify-pass waiver, critic `pass-risk`, terminal gate not CONFIRMED, or a
> non-pass `reverify` stamp). Those are real wins that still need a human call; route them
> to a person, don't ship them.

`mergeable:true` = the strongest evidence ARO produces (random-input differential proved the
output byte-identical **and** the critic passed clean **and**, when the target declares
`terminal_bench_targets`, the criterion row-level Ir terminal gate returned
`TERMINAL_CONFIRMED`). That's safe to *propose*. Everything below gates on it.

---

## 0. Ship gate (mandatory — before any packaging)

The terminal stamp certifies criterion-Ir wins against a specific `baseline_sha`. The PR
targets some remote branch head. Those two **must agree**. Never hand-rebase certified
edits onto a moved baseline: mega-evm PR #346 shipped never-replayed bytes after main
moved under an editable region during the campaign; CI caught a real panic.

```sh
python3 -m aro ship gate targets/<spec>.json --manifest .aro-runs/<RUN>
# optional: --target origin/main  (default: spec ship_target or origin/main)
# optional: --no-fetch            (resolve the ref locally; default fetches first)
```

| result | action |
|---|---|
| **PASS** | clearance: stamp baseline == target head. Proceed to §1. |
| **FAIL** | **do not ship.** Follow the printed re-certification sequence (re-pin `baseline_ref` → `aro recheck candidates` full-chain replay → re-measure terminal on survivors). Do not hand-rebase. |
| **ERROR** (fetch failed / no mergeable / legacy stamp without `baseline_sha`) | exit 1, fail-closed. Fix the environment or re-measure with current aro. |

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
  `quarantine` (outlier tripwire reason), `reverify` (from `aro recheck candidates --apply`),
  optional `regime_waiver` (set to `"reverify-pass"` when a non-byte-identical regime
  was waived by a reverify-pass stamp), and when an outlier was cleared:
  `quarantine_disclosure: "required"` plus `quarantine_cleared_by:
  "human-audit" | "auto-evidence"`.
- top-level `terminal` (when present): the whole-checkout stamp shared by the bundle
  (includes `terminal_stamp` when tool-written).

Split:
- **`mergeable:true`** → candidates for this PR.
- **`mergeable:false`** → do NOT PR. Collect them into a short "needs human review" note
  (fn · Δ · regime · critic · terminal · quarantine · reverify + `hypothesis`) and hand
  that to a person instead.

**Quarantine hard rule:** an entry with a `quarantine` field (outlier `|Δ|` tripwire;
default-on at 5% even when the target omits `outlier_quarantine_pct`) is blocked unless
a clear path fired inside `resolve_mergeability`:

1. **Valid `quarantine_audit`** (human ruling via `aro manifest --clear-quarantine`;
   stale if Δ drifted >0.5pp) — never write the audit record on your own initiative.
2. Else **complete mechanical evidence**: `reverify.verdict == "reverify-pass"`
   (hardened-gate replay). Auto-clears; never fabricates an audit.

Entries with `quarantine_disclosure: "required"` **are packageable** (when
`mergeable:true`). The PR body **MUST** contain an **"Outlier disclosure"** section —
one block per disclosed entry covering: fn, Δ%, hypothesis, gate records
(`reverify-pass`, probe sha if present), cleared-by (`human-audit` with by/date/evidence,
or `auto-evidence`), and review-attention notes (e.g. unsafe blocks).

Blocked outliers (no valid audit **and** no `reverify-pass`) still never package — route
those to a human; that is now the **only** outlier case that escalates. Same hard rule
for any `reverify.verdict` other than `reverify-pass` (reverify-fail stays forced-false
regardless of audit).

**Relaxed + reverify-pass:** a campaign-time `regime: "relaxed"` entry that later
gains `reverify.verdict == "reverify-pass"` (replay under current hardened gates)
has the regime block **waived** — `mergeable` is then decided by the remaining gates
(critic / terminal / quarantine). The `regime` field itself is **not** rewritten
(provenance); look for stamp `regime_waiver: "reverify-pass"` on the entry. A
relaxed entry without reverify-pass stays `mergeable:false` exactly as before.

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

### Verdict decision table (prescriptive — follow; do not re-litigate)

Every terminal verdict is a **work order**. Next actions and autonomy levels:

| Verdict | Next action (exact) | Autonomy |
|---|---|---|
| `TERMINAL_CONFIRMED` | stamp (`--update-manifest`) → run-to-pr | **autonomous** (human point = PR review) |
| `TERMINAL_CONFIRMED_WITH_TRADE` | stamp → run-to-pr; PR body MUST list every traded regression (row, Δ%, cap) | **autonomous** |
| `TERMINAL_MIXED` | **work order, not a question**: `aro ablate` on the bundle → drop entries per the keep/drop proposal → re-run terminal on the pruned shipping set → re-enter this table with the new verdict. There is NO manual release path for MIXED — the release path for "net positive within policy" is `TERMINAL_CONFIRMED_WITH_TRADE`, produced by the tool or not at all. | **autonomous loop**; escalate only if ablate's proposal is empty or two prune→re-terminal iterations fail to converge |
| `TERMINAL_REGRESSED` | no PR; record the terminal doc; candidates stay non-mergeable; close out with a report | **autonomous** |
| `TERMINAL_UNTOUCHED` | no PR (criterion rows did not move); candidates go to the frozen / sub-resolution pool per the standing instrument protocol | **autonomous** |
| `TERMINAL_TEST_FAILED` | drop the offending entry (recheck `--apply` demotes it), re-run terminal on the remaining set → re-enter this table | **autonomous** |
| `TERMINAL_CONTROL_ANOMALY` | run the A/A disambiguation protocol FIRST; never touch `control_composition_bound_pct` on your own — escalate WITH the A/A evidence attached | **escalate after A/A** (bound changes are a policy ratchet) |

**WITH_TRADE detail:** reachable **only** when the target declares the row-family policy
fields (`protected_row_families`, `tradeable_regression_cap_pct`). PR body MUST list every
traded regression verbatim from terminal notes (`traded: <row> <Δ%> (cap Y%)`).
Protected-family band rows (`band: …`) may appear in notes but do not block the verdict.

**Escalate ONLY when** (exhaustive list; everything else follows the table):
1. Integrity anomaly — verdict contradicts row data, tool behaves impossibly, artifacts disagree with each other.
2. Policy ratchet — any change to bounds/caps/protected families/thresholds (requires evidence, e.g. A/A).
3. Outlier-quarantine adjudication — the audit packet is prepared by the operator, the in/out ruling is human.
4. PR review/merge — always human.

**Blame-free clause:** Following this table is never an operator fault, even when the outcome is bad — a wrong prescription is a defect of the table, to be reported and amended, not a reason to stop and ask.

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

## 2. Package the certified set (`aro ship package`)

**Enforcement is the command** — it inlines the gate, builds the branch, applies
every `mergeable:true` patch in acceptance order (exact unique-SEARCH, same
machinery as reverify/ablate replay), makes **one** certified-set commit, and
writes `<run>/pr_body.md`. Manual worktree + hand-apply + hand-written body is
how #346/#347 skipped steps; do not bypass the command.

```sh
python3 -m aro ship package targets/<spec>.json --manifest .aro-runs/<RUN>
# optional: --branch aro/ship-<run>   (default: aro/ship-<runname>)
# optional: --workdir <dir>           (default: <repo.parent>/.aro-worktrees/ship-<runname>)
# optional: --target / --no-fetch     (same semantics as ship gate)
```

| step | what the command does |
|---|---|
| inline gate | runs `ship gate` first; non-PASS → abort (no worktree) |
| worktree | at the gate-verified target head; submodules offline like `make_worktree` |
| apply | every `mergeable:true` patch in `order`; any SEARCH miss/ambiguity → **integrity error**, names the order |
| commit | single `perf: ARO <n>-edit certified set (<verdict>)` (+ body: campaign run + stamp sha256) |
| body | writes `<run>/pr_body.md` (Summary / Delta Ir-first **excluding control lanes** / Traded regressions / Outlier disclosure / Provenance / Files changed) |

**Why control lanes are excluded from Delta headlines:** control drift is not a
product win — putting it in the PR table was the #347 lesson. Lane list comes
from spec `control_lanes`.

**What you still need to understand** (prose; the command enforces apply order
and body sections, not your judgment):

- SEARCH must appear exactly once; no fuzzy apply. A miss means baseline drift
  relative to the stamp — re-certify, do not force.
- Outlier disclosure blocks (`quarantine_disclosure: "required"`) are generated
  for both `human-audit` and `auto-evidence` clear paths; review-attention notes
  fire when patch text contains `unsafe`.
- WITH_TRADE requires the Traded regressions table (row, Δ%, cap) verbatim from
  terminal notes.

Prints workdir, branch, body path, and the next steps below.

---

## 3. Supplements (dual-green) + fmt — then ship conformance

After package, the branch has **only** the certified-set commit. Allowed
post-cert commits (see `pr-discipline.md`):

| commit | subject pattern | rule |
|---|---|---|
| Supplementary tests | `test(<crate>): …` or `test: …` | dual-green on baseline **and** PR branch; real assertions; never edit src to make a test pass |
| Mechanical formatting | `style: cargo fmt` | run `cargo fmt` twice (second = no diff); one commit |

Anything else on the branch after the certified-set commit is refused by
`ship open` (whitelist). Coverage/mutation evidence still follows
`pr-discipline.md` (CI-adjudicated when too heavy for local `ship_conformance`).

Then re-prove quality on the final bytes:

```sh
# workdir = the package worktree after dual-green + optional fmt commits
python3 -m aro ship conformance targets/<spec>.json --workdir <branch-checkout>
# optional: --out /path/to/record.json   (default: <workdir>/.aro-conformance.json)
```

| preflight / result | action |
|---|---|
| spec has no/empty `ship_conformance` | exit 1 — define the list (see `spec-slots.md`); no silent empty-pass |
| workdir not a git checkout, or has **uncommitted tracked** dirt | exit 1 — record must bind to committed bytes only |
| any check non-zero / timeout | exit 1 — print per-check verdict table; record still written with every check's exit/duration/tail |
| all checks exit 0 | exit 0 — `all_green: true`, record cites `head_sha` |

**Non-green → do not open.** Fix via the allowed post-cert loop, re-run until green.
The open gate requires this record bound to the **current** HEAD (a record for
older bytes is stale → refuse).

What the checks *mean* (explanation; the command is enforcement): re-prove
compile + test on the branch you will open — ARO already proved correctness + Ir
on worktrees; this confirms the patch lands cleanly on the packaging branch.

---

## 4. Open the PR (`aro ship open`)

**Opening a PR without a green conformance record is now impossible, not just
forbidden.** `ship open` is the machine gate; do not call `gh pr create` by hand.

```sh
python3 -m aro ship open targets/<spec>.json \
  --manifest .aro-runs/<RUN> \
  --workdir <branch-checkout>
# optional: --record <path>   (default: <workdir>/.aro-conformance.json)
# optional: --title "perf(…): …"
# optional: --target / --no-fetch
```

All checks must pass (fail-closed, exit 1 with the specific reason):

1. Re-run `ship gate` → PASS (baseline still current at open time).
2. Conformance record exists, `all_green: true`, `head_sha` == workdir `HEAD`.
3. Workdir has no uncommitted tracked changes.
4. Commits after the certified-set commit each match `^test(\(|:)` or
   `^style: cargo fmt`.
5. Branch is not the ship-target branch itself.

Then: `git push -u <ship_remote|origin> <branch>` and
`gh pr create --title … --body-file <run>/pr_body.md --base <ship_target branch>`
plus `--label` for each entry in optional spec `pr_labels` (missing labels
caused mega-evm require-label CI red in #346). Prints the PR URL.

> **Language:** PR title and body in English. Default title is the certified-set
> commit subject; override with `--title` for house style
> (`perf(<crate>): <what> (<X% fewer instructions on <row>>)` — headline X from
> subject Ir rows, not wall-clock).

Do **not** enable auto-merge. Human review is the merge gate.

---

## 6. Safety rails (recap)

1. PR **only** `mergeable:true`. 🟡 → human, never auto-PR.
2. Exact SEARCH match or **stop** (no fuzzy apply) — enforced by `ship package`.
3. **Step order:** gate → package → supplements (dual-green) + fmt →
   conformance → **open**. Never skip with hand `gh pr create`.
4. **`aro ship open` refuses** without a green conformance record bound to
   current HEAD, a clean tree, and a post-cert commit whitelist.
5. **Cover the changed lines** with meaningful tests so the patch-coverage CI passes: real
   assertions, never coverage-padding. Tests go in the PR diff, not ARO's report;
   dual-green on baseline and PR branch (`pr-discipline.md`).
6. PR is a proposal: never auto-merge; keep the "generated by an automated agent" footer.
7. Every claim/number on the PR comes from artifacts (`manifest.json` /
   `terminal_stamp` path+sha256 / verified `terminal.json` rows / conformance
   record / the spec), never from memory or free-form narrative. Do not invent
   performance numbers.

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

So: after terminal CONFIRMED on the mergeable set →
`aro ship package targets/mega-evm-v2.json --manifest .aro-runs/<RUN>` builds
`aro/ship-<run>` with the certified commit + `pr_body.md`. The edit adds a new
`if is_oracle { … } else { … }` tail branch in `sload`, so **cover both sides**
with a dual-green `test(mega-evm): …` commit, then
`aro ship conformance … --workdir <wt>` → `aro ship open …`. Title e.g.
`perf(mega-evm): hoist redundant SLOAD oracle predicate (N% fewer instructions on <row>)`.
One clean PR; the 3 relaxed wins go to a human out-of-band, NOT mentioned in the PR.

---

## 8. After the PR exists (HARD RULE — byte-frozen branch)

**The PR branch is byte-frozen once opened.** No hand edits — by human or agent — for
any reason, including review nits. The bytes on the branch are exactly the
re-certified, conformance-green set that `aro ship gate` + `aro ship conformance`
cleared. Patching that branch after open re-introduces the #346 class of failure
(shipped bytes that never re-entered the measurement loop).

### Revisions re-enter the loop (re-certified revision path)

When review asks for changes (or the PR is closed unmerged with actionable
comments), do **not** push a fixup commit onto the open branch. Instead:

1. **Harvest** — `python3 -m aro ship watch targets/<spec>.json --manifest .aro-runs/<RUN> --pr <url-or-number>`
   writes `<run>/pr_feedback/<pr>.json` and seeds `<run>/reattempt-queue.json`
   (one pending seed per path-bound comment). Never auto-runs a campaign.
2. **Amend the bundle** from the harvested hints (new attempt / re-derive on
   current baseline as needed).
3. **`aro recheck candidates`** full-chain replay on the amended set.
4. **Terminal re-measure** on survivors; re-stamp the manifest.
5. **`aro ship gate` + `aro ship conformance`** on the **new** bytes (new stamp,
   new conformance record bound to the new `head_sha`).
6. **Force-push only the re-certified diff.** The operator cites the new
   `terminal_stamp` + new conformance record in a PR comment. That is the only
   legal update to the PR branch after open.

### Post-merge follow-ups

Follow-ups after merge are a **new campaign on the new baseline** — never patch
the merged PR's branch. The watch **merged** verdict stamps every
`mergeable:true` entry with `shipped: {pr, state: "merged", merge_sha}` so the
campaign ledger knows those bytes landed.

### Watch cadence (mandatory follow-through)

After opening, run `aro ship watch` on a cadence (cron / operator) or when the
user reports an outcome:

| outcome | action |
|---|---|
| **merged** | stamp `shipped` on mergeable entries (idempotent upsert) |
| **closed** (unmerged) | harvest feedback + seed reattempt queue; manifest untouched |
| **CHANGES_REQUESTED** (open) | same harvest + queue; PR stays open awaiting re-certified revision |
| **open**, no feedback | no-op |

Merged and closed both have **mandatory** follow-through (stamp / harvest).
Leaving either un-watched loses the loop closure the PR-as-review workflow needs.
