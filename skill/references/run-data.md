# ARO run-data contract: where the data is, what it means, how to consume it

What an ARO run writes to its `--out-dir`, what every file/field means, and how a
downstream agent turns a run into a PR. Self-contained: you can act on a run by reading
only this doc + the run's files. **The source of truth is `events.jsonl`**: everything
else is derived from it and can be regenerated.

---

## 0. TL;DR: "turn a run into a PR"

Don't reverse-engineer the event log. Run this (no LLM, no cost; works on any run, old
or new):

```bash
python3 -m aro manifest <out-dir>      # writes <out-dir>/manifest.json (auto-written at run end too)
```

`manifest.json` IS the hand-off artifact: the final accepted edits the run folded into
its baseline, each with `attempt` dir, candidate `id`, `fn`, `files`, `delta_pct`,
`regime`, `critic_verdict`, **`mergeable`**, `hypothesis`, and `patch_path`. Then:

1. Apply each accepted patch **in `order`** (they compound) on `baseline_ref`.
2. **Only PR `mergeable:true` entries directly.** The rest need a human call (see §5).
3. Patch text is at `patch_path` (SEARCH/REPLACE blocks, §4).

That's it for the common case. The rest of this doc is the full contract.

---

## 1. Out-dir layout

```
<out-dir>/
  events.jsonl            ← SOURCE OF TRUTH. append-only JSON-lines event stream (§3)
  manifest.json           ← final accepted edit-set + provenance + mergeable flag (§0, aro/manifest.py)
  REPORT.md               ← human text report (realized / headroom / floor / decision), live-refreshed
  decision-tree.html      ← self-contained interactive report (open in a browser; embeds its own data)
  tree.json               ← the data decision-tree.html renders (derived from events.jsonl)
  trajectory.svg / .png   ← realized-vs-headroom line over the run
  perf-token.svg / .png   ← running-best speedup vs cumulative LLM tokens
  a1/ a2/ … aN/           ← ONE dir per attempt (a sweep optimizes functions one at a time) (§2)
```

A plain `aro run` (single target, no frontier walk) writes the attempt files at the
**root** instead of in `a<N>/` dirs.

Everything except `events.jsonl` is **derived**: regenerate any of it with
`python3 -m aro tree <out-dir>` (report) or `python3 -m aro manifest <out-dir>`
(manifest). Neither re-runs the optimization or costs anything.

---

## 2. The per-attempt dir `a<N>/`

A `sweep --attempt` run walks hot functions one at a time; attempt *N* is `a<N>/`, a
self-contained record store (`aro/store.py` `Memory`):

| file | meaning |
|---|---|
| `records.jsonl` | one row per candidate this attempt judged: `{id, verdict, hypothesis, metrics[], notes[]}` (a convenience view; `events.jsonl` is authoritative) |
| `patches/<id>.txt` | the candidate's patch: SEARCH/REPLACE blocks, or `NoOp` (§4) |
| `pareto.txt` | candidate ids the judge ACCEPTED this attempt, one per line |
| `floors.json` | the A/A-calibrated noise floors used to judge significance |
| `agenda.jsonl` | reflect-proposed next-step research directions |

**`base-*` ids are NOT candidates.** When a run resumes/compounds, the already-accepted
edits are seeded into the next attempt's store as `base-0`, `base-1`, … (in
`patches/` and `pareto.txt`). Skip any id starting with `base-` when reading candidates.

---

## 3. `events.jsonl`: the source of truth

One JSON object per line, appended, flushed immediately (tailable live). **Envelope on
every line:**

| field | meaning |
|---|---|
| `seq` | monotonic order within the file (the only reliable ordering) |
| `run_id` | the run this event belongs to. The file is appended across re-runs; **a report/manifest uses the *latest* `run_id`'s slice.** A whole sweep shares ONE `run_id` (it is the meta-run, NOT per-attempt) |
| `ts`, `elapsed_s` | wall-clock timestamp and seconds since run start |
| `event` | the event type (below) |
| `attempt` | **(new runs)** the `a<N>` index this event belongs to, stamped on every event inside a sweep attempt's backtest. Absent on old runs and on between-attempt events (see §6) |

**Event catalog** (field list = that event's own fields, beyond the envelope):

| event | when | key fields |
|---|---|---|
| `run_started` | each attempt's backtest starts | `target`, `baseline_ref`, `rounds`, `aa_runs`, `ab_pairs` |
| `attempt_frontier` | sweep start | `fns`, `untried`, `budget`, `policy` |
| `profile_floor` | sweep start | `frames` (untouchable crypto/runtime frames) |
| `attempt_started` | a function attempt begins | `fn`, `pct`, `regime` (`byte-identical` / `relaxed` / `micro-proven`: the last = judged under a qualified isolation micro-bench; **never auto-mergeable**), `files`, `try_n`, `probe` (sha prefix, micro-proven only) |
| `baseline_built` / `floors_calibrated` / `regression_baseline` | setup | `worktree` / `floors` / `n_pre` |
| `round_started` | a round in an attempt | `round`, `accepted_so_far`, `memory_summary` |
| `read_phase` / `reflect` | LLM read/reflect steps | `round`, `tokens` |
| `candidate_proposed` | generator produced a candidate | `id`, `hypothesis`, `lens`, `files`, `tokens`, `cost_usd`, `round` |
| `prescreen` / `prescreen_ordered` | cheap pre-judge filter | `id`, `ok`, `smoke_delta` / `order` |
| `critic` | 2nd judge (semantic reviewer) on a candidate | `id`, `verdict` (`pass`/`pass-risk`/`reject`), `reasons[]`, `kind`, `tokens` |
| `gate` | one deterministic gate result | `candidate`, `gate` (guard/apply/build/test/differential/significance), `status`, `detail` |
| `bench_rescaled` | auto-tighten re-bench | `candidate`, `from_scale`, `to_scale` |
| `candidate_verdict` | final verdict for a candidate | `id`, `verdict`, `deltas[]` (per-metric `{metric, delta_pct, ci_low_pct, ci_high_pct, floor_pct, improved, regressed}`) |
| **`baseline_advanced`** | **a candidate was FOLDED into the compounding baseline (a real win)** | `by` (the winning id), `cumulative_edits` (running count), `files` (cumulative file list) |
| `direction_proposed` / `direction_resolved` | reflect agenda | `id`, `direction`, `source` / `status` |
| `attempt_finished` | a function attempt ends | `fn`, `verdict`, `delta`, `accepted`, `regime`; **additive final operator checkpoint** when the last round's results were not already flushed by a later `round_started`: `memory_summary`, `accepted_so_far` (same payload shape as `round_started`; see `runlog.operator_checkpoints`) |
| `explore_step` | per-attempt explorer decision | `i`, `decision`, `reason`, `realized_pct`, `headroom_pct`, `floor_pct` |
| `attempt_resweep` / `attempt_skipped` / `attempt_exhausted` | frontier bookkeeping | `remaining` / `fn`,`reason` / `policy` |
| `generator_error` | a generation-side failure (traceable: a broken generator must not look like "no proposal") | `generator` (ralph/agentic), `stage` (worktree/seed/seed-commit/claude/codex/grok/parse/diff/read/reflect), `k`, `detail` |
| `parent_coverage` | L4a pre-check: does the PARENT differential constrain this fn? (seeded mutation must alarm) | `fn`, `covered` (true/false/null=unverifiable) |
| `probe_registered` | L4a probe-judge verdict on an authored micro-bench, **frozen before any candidate generation** | `fn`, `ok`, `path`, `sha256`, `floor_pct`, `parent_floor_pct`, `relevance_pct`, `scale_ratio`, `reasons[]` |
| `parent_check` | a micro-proven win's parent-workload non-regression gate before folding | `fn`, `regressed`, `deltas[]` |
| `run_finished` | attempt backtest ends | `pareto`, `accepted`, `candidates`; **additive final operator checkpoint** when needed: `memory_summary`, `accepted_so_far` (last-round accepts no longer require a subsequent `round_started` to surface) |
| `decision_tree_written` / `manifest_failed` / … | finalize | derived-artifact status |

**The wins are `baseline_advanced` events.** Their `by` id + `attempt` → the patch at
`a<attempt>/patches/<by>.txt`. A candidate that was judge-accepted but superseded by a
better sibling does NOT get a `baseline_advanced` (it stays in `pareto.txt` only), so it
is correctly excluded from the final change set.

---

## 4. Patch format

`patches/<id>.txt` is either the literal `NoOp`, or one or more whole-file edits:

```
--- edit 1 ---
path: crates/mega-evm/src/evm/host.rs
<<<<<<< SEARCH
<exact text to find, appears once in the baseline file>
=======
<replacement text>
>>>>>>> REPLACE
```

These are anchored to the **baseline** content (`baseline_ref`), and later wins anchor to
the state produced by earlier wins (they compound). So apply them in `order` on
`baseline_ref`. They are NOT git unified diffs: to get a `.patch`, apply the blocks then
`git diff`. Parser: `aro/store.py` `_parse_patch_file`.

---

## 5. Semantics that matter before you merge

**`accepted` ≠ should-merge.** The judge proved correctness + a real speedup; it did NOT
decide the change is good engineering. Two fields gate merge-readiness, both surfaced in
the manifest as **`mergeable`**:

- **`regime`**: `byte-identical` means a random-input differential proved the output is
  bit-for-bit unchanged (safe). `relaxed` means the function was architecture-gated /
  ran without a byte-identical differential: the win is real but behavior-equivalence is
  NOT byte-proven; a human must judge it.
- **`critic_verdict`**: the 2nd judge (semantic reviewer): `pass` (clean), `pass-risk`
  (passed but flagged a reservation: read `reasons[]`), `reject` (blocked; never folded).
  `null` means the critic was off for that run.

`mergeable = (regime == "byte-identical") AND (critic_verdict in {null, "pass"})`
(plus `terminal == TERMINAL_CONFIRMED` when the target declares `terminal_bench_targets`).
PR the `mergeable:true` entries directly; route the rest to a human with their `regime` +
critic `reasons[]` attached. (Reward-hacks the critic caught are `reject`-ed candidates
in `events.jsonl`: never in the manifest, but visible if you audit.)

**Outlier quarantine (default-on tripwire).** After the regime/critic/terminal rule, an
accepted entry whose best `|delta_pct|` exceeds `outlier_quarantine_pct` (default **5.0
even when the field is absent** on the target JSON; explicit `0` disables) is forced to
`mergeable=false` with an additive field:

```json
"quarantine": "outlier: |Δ|=19.150% > 5.0%"
```

Applied in both `build_manifest` and `apply_terminal` so the paths cannot diverge. Never
auto-promotes `mergeable`. A quarantined entry must **never** be packaged into a PR —
treat it as needs-human-review (often a semantics bypass that still cleared the judge).
See `docs/OPERATIONS.md` §13.2 / `aro/manifest.py`.

**`reverify` stamp (optional, from `aro recheck candidates --apply`).** After a gate-hardening
deploy, re-adjudication may stamp each accepted entry:

```json
"reverify": {"verdict": "reverify-pass"}
// or
"reverify": {"verdict": "reverify-fail", "failing_gate": "differential"}
```

Non-`reverify-pass` forces `mergeable=false`. A pass never auto-promotes `mergeable=true`.
Full replay semantics: `docs/OPERATIONS.md` §13.7.

---

## 6. The id-collision gotcha (and how it's handled)

Candidate ids are per-round local: `agent-r0-0`, `agent-r1-1`, … so **the same id exists
in every attempt dir**, and `run_id` does NOT disambiguate (a whole sweep shares one).
So a `baseline_advanced{by:"agent-r0-0"}` is ambiguous on its own.

- **New runs** stamp `attempt` on every event → `a<attempt>/patches/<by>.txt` directly.
- **Old runs** (no stamp): derive the attempt by counting `attempt_started` events in
  `seq` order up to the event. `aro/manifest.py` does this for you (it prefers the stamp,
  falls back to counting), so **just use `manifest.json`** rather than re-deriving.

---

## 7. Cross-run memory

`memory/lessons.jsonl` (in the aro-py repo, NOT the out-dir) is the durable cross-run
ledger: every candidate's `{target, change, verdict, delta, note}`. A later sweep recalls
it to skip known dead ends and mark already-tried functions. It is shared across all
targets; it is not part of a single run's out-dir.

---

## 8. Recipes

```bash
# Final change set + merge-readiness (the hand-off):
python3 -m aro manifest <out-dir>            # → manifest.json (§0)

# Re-render the human report from the truth (no re-run, no cost):
python3 -m aro tree <out-dir>                # → decision-tree.html + tree.json

# Serve the report on a server (live-refreshes from events.jsonl):
python3 -m aro serve <out-dir> --port 8010

# Resume / compound: point a new run at the SAME out-dir → it continues from the
# advanced baseline (needs the out-dir intact + the target repo at baseline_ref).
python3 -m aro sweep <spec.json> --attempt --out-dir <out-dir>
```

**Invariant for any consumer:** trust `events.jsonl`. `manifest.json` / `tree.json` /
`REPORT.md` are conveniences derived from it; if they ever disagree, the event log wins,
and you can rebuild them.
