# aro daily-report: render a round's human daily report from its artifacts

Turn ONE explore round's run directory into the human-facing daily optimization report
defined by `daily-report-template.md`. This is a **view of the event log**, never a
re-judgement: every number is copied verbatim from `events.jsonl`, a within-noise/regressed
result is never laundered into a win, and "accepted" is reported as *correctness+speed
proven*, **not** should-merge. The report's job is to answer four things, **what changed /
how much it improved / what code changed / what to do next**, readably, and to hand the
human the regime decisions only they can make.

## When to use

After a round of `python3 -m aro sweep <spec> --attempt --diverge … --out-dir DIR`
finishes (it self-stops or hits budget). Inputs under `DIR`:

| artifact | gives |
|---|---|
| `events.jsonl` | the verbatim run-log: every attempt + verdict + Δ + the explore steps |
| `a{N}/records.jsonl` | each attempt's candidate hypothesis (the "what changed" prose) |
| `a{N}/patches/agent-r0.txt` | the accepted patch (the "what code changed" diff) |
| `trajectory.svg` | the realized vs addressable-headroom chart (embed it) |
| `REPORT.md` | the machine report (realized/headroom/floor/decision), cross-check only |

## Steps

1. **Take the latest run slice.** Read `events.jsonl`; keep only events whose `run_id`
   equals the last `run_started`'s. Parse:
   - `attempt_finished` → `(fn, verdict, delta, accepted, regime)` per attempt.
   - `explore_step` → `(realized_pct, headroom_pct, unreachable_pct, floor_pct, decision, reason)`; the last one is the round's end-state.
   - `attempt_skipped` → the unreachable (un-locatable) function names.
2. **Per accepted attempt**, open its `a{N}/records.jsonl` (the `accepted` row's
   `hypothesis`) and `a{N}/patches/agent-r0.txt`. Distil:
   - **What changed** = the hypothesis in one clause (what was eliminated/weakened).
   - **Why it was waste** = why the removed work was redundant (from the hypothesis).
   - **File** = the `path:` in the patch; **code** = a one-line summary of the SEARCH→REPLACE
     (signature change / call rewrite / removed load), with the patch path for the full diff.
3. **Fill the template** (`daily-report-template.md`):
   - **Describe the workload in plain language, never just the probe filename.** `evm_r3`
     means nothing to a reader. Read the spec's `benchmark_probe.probe` file's `//!` doc
     header (it says what the bench drives) and write one clause of what it actually does
     (e.g. "storage hot-path micro-benchmark: repeated SSTORE/SLOAD on 4 resident slots,
     hitting the slot-present branch of inspect_storage"); put the raw probe path in parens.
   - TL;DR: `n_attempts` (located attempts), `n_accepts`, `realized`, `decision` + `reason`.
     If any accept is `relaxed`, add a `relaxed_note`: "This optimization is in the
     **relaxed regime** (it changed structure and should not be merged directly; a human
     must decide)". Never use bare jargon ("win" / "relaxed" / "should-not-merge") in the
     reader-facing prose: write "optimization" / "proven optimization", "relaxed regime",
     "should not be merged directly" (gloss the jargon term once).
   - Section 1: the accepts table + the per-accept code block; the **tried but did not
     pass** table (every `within-noise`/`regressed` attempt, with a one-line why); the
     **skipped** line.
   - Section 2: the chart + the realized/addressable/unreachable/floor table (verbatim).
4. **Synthesize Section 3 (what to do next) deterministically, from the end-state. Do NOT pad.**
   Emit only the directions the data actually supports:
   - **D1 keep mining the addressable**: iff `addressable ≥ 2%`. Unlocks ≤`addressable`%.
     Cost low, byte-identical. Owner: **automatable**.
   - **D2 adopt and dig deeper into relaxed**: iff any accept is `relaxed` OR the `gated`
     bucket is non-empty. Unlocks the relaxed area. Cost: a weaker guarantee (architecture
     change). Owner: **your call**.
   - **D3 make the unreachable nameable**: iff `unreachable ≥ 5%`. Unlocks that %. Cost:
     tooling work (inline-aware profiling / refactor the macro that generates the handlers).
     Owner: **your call**.
   - **D4 switch workload**: iff `decision == STOP` OR `addressable < 2%`. Unlocks unknown
     (new hot paths). Cost: you must supply a representative new workload. Owner: **your call**.
   If the round is fully drained (only D4 survives), say so plainly: "this workload is
   drained under the strict regime; the only real option is to switch regime". Never
   invent options to fill the table.
5. **The "decisions you need to make now" section**: the human gates:
   - For each `relaxed` accept: "Adopt the `<fn>` <Δ> optimization? (It is in the relaxed
     regime: it changed structure and should not be merged directly.) Adopt → it enters the
     persistent baseline and later rounds run on top of it; decline → it is dropped."
   - "Which one runs next round?" listing the surviving direction ids.

## Honesty rules (the moat, restated for the report)

- **Numbers verbatim.** Every Δ / realized / headroom is copied from `events.jsonl`. If
  the report and `REPORT.md` disagree, the event log wins and you have a bug to flag;
  do not silently pick the prettier number.
- **Record the dead ends.** The **tried but did not pass** table is mandatory when there
  were non-accepts. A round that only lists wins is hiding the cost of the search (mirror
  the reference doc's "Vec to field: zero change → direction closed" entry).
- **`accepted` ≠ should-merge.** Report a `relaxed` win as a human decision, never as a
  merged improvement. The judge weighed correctness+speed, not architecture.
- **No padded directions.** Section 3 lists only what the end-state supports; "drained"
  is a valid, honest conclusion.

## Output + optional Lark push

- Write the filled report to `DIR/DAILY-REPORT.md`.
- **Always render a PNG and embed THAT, not the `.svg`**: markdown previewers don't render
  SVG, so `![](trajectory.svg)` shows nothing. Do:
  `qlmanage -t -s 1100 -o DIR DIR/trajectory.svg && mv DIR/trajectory.svg.png DIR/trajectory.png`
  (macOS), then `![trajectory](trajectory.png)` in the report.
- To publish to Lark, switch to the `lark-doc` skill: `docs +create --api-version v2`
  with the markdown, then `docs +media-insert` the PNG. **Publishing is an outward action;
  confirm with the user first** (and per global rules, never post without approval).
