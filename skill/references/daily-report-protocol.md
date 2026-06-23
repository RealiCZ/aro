# aro daily-report — render a round's human日报 from its artifacts

Turn ONE explore round's run directory into the human-facing 优化日报 defined by
`daily-report-template.md`. This is a **view of the event log**, never a re-judgement:
every number is copied verbatim from `events.jsonl`, a within-noise/regressed result is
never laundered into a win, and "accepted" is reported as *correctness+speed proven*,
**not** should-merge. The report's job is to answer four things — **改了什么 / 提升了多少
/ 改了什么代码 / 后续做什么** — readably, and to hand the human the regime decisions only
they can make.

## When to use

After a round of `python3 -m aro sweep <spec> --attempt --diverge … --out-dir DIR`
finishes (it self-stops or hits budget). Inputs under `DIR`:

| artifact | gives |
|---|---|
| `events.jsonl` | the verbatim run-log — every attempt + verdict + Δ + the explore steps |
| `a{N}/records.jsonl` | each attempt's candidate hypothesis (the "改了什么" prose) |
| `a{N}/patches/agent-r0.txt` | the accepted patch (the "改了什么代码" diff) |
| `trajectory.svg` | the 进化了 vs 能进化的 chart (embed it) |
| `REPORT.md` | the machine report (realized/headroom/floor/decision) — cross-check only |

## Steps

1. **Take the latest run slice.** Read `events.jsonl`; keep only events whose `run_id`
   equals the last `run_started`'s. Parse:
   - `attempt_finished` → `(fn, verdict, delta, accepted, regime)` per attempt.
   - `explore_step` → `(realized_pct, headroom_pct, unreachable_pct, floor_pct, decision, reason)` — the last one is the round's end-state.
   - `attempt_skipped` → the unreachable (un-locatable) function names.
2. **Per accepted attempt**, open its `a{N}/records.jsonl` (the `accepted` row's
   `hypothesis`) and `a{N}/patches/agent-r0.txt`. Distil:
   - **改了什么** = the hypothesis in one clause (what was eliminated/weakened).
   - **为什么是浪费** = why the removed work was redundant (from the hypothesis).
   - **文件** = the `path:` in the patch; **代码** = a one-line summary of the SEARCH→REPLACE
     (signature change / call rewrite / removed load), with the patch path for the full diff.
3. **Fill the template** (`daily-report-template.md`):
   - **Describe the workload in plain language — never just the probe filename.** `evm_r3`
     means nothing to a reader. Read the spec's `benchmark_probe.probe` file's `//!` doc
     header (it says what the bench drives) and write one clause of what it actually does
     (e.g. "存储热路径微基准:对 4 个常驻槽反复 SSTORE/SLOAD,命中 inspect_storage 的
     slot-present 分支"); put the raw probe path in parens.
   - TL;DR: `n_attempts` (located attempts), `n_accepts`, `realized`, `decision` + `reason`.
     If any accept is `relaxed`, add a `relaxed_note`: "此优化属**放宽档**(动了结构、不该直接合,要人定夺)". Never use the bare jargon "赢" / "relaxed" / "should-not-merge" in the
     reader-facing prose — say 优化/优化成功, 放宽档, 不该直接合 (gloss the English term once).
   - §一 accepts table + the per-accept code block; the **试了但没过** table (every
     `within-noise`/`regressed` attempt, with a one-line why); the **跳过** line.
   - §二 the chart + the realized/addressable/unreachable/floor table (verbatim).
4. **Synthesize §三 后续方向 — deterministically, from the end-state. Do NOT pad.**
   Emit only the directions the data actually supports:
   - **D1 继续挖可达** — iff `addressable ≥ 2%`. Unlocks ≤`addressable`%. Cost low,
     byte-identical. Owner: **可自动**.
   - **D2 采纳并深挖 relaxed** — iff any accept is `relaxed` OR the `gated` bucket is
     non-empty. Unlocks the relaxed area. Cost: 担保变弱(架构改). Owner: **要你拍板**.
   - **D3 让够不着的可命名** — iff `unreachable ≥ 5%`. Unlocks that %. Cost: 工具活
     (inline-aware profiling / refactor the macro that generates the handlers). Owner: **要你拍板**.
   - **D4 换 workload** — iff `decision == STOP` OR `addressable < 2%`. Unlocks unknown
     (new hot paths). Cost: 要给代表性新负载. Owner: **要你拍板**.
   If the round is fully drained (only D4 survives), say so plainly: "本 workload 在强
   regime 下已榨干,真选项只有换 regime" — never invent options to fill the table.
5. **§需要你现在拍板** — the human gates:
   - For each `relaxed` accept: "采纳 `<fn>` <Δ> 这处优化吗?(它是放宽档:动了结构、不该
     直接合)采纳 → 进持久 baseline,后续轮在它上面跑;不采纳 → 丢弃。"
   - "下一轮跑哪个?" listing the surviving direction ids.

## Honesty rules (the moat, restated for the report)

- **Numbers verbatim.** Every Δ / realized / headroom is copied from `events.jsonl`. If
  the report and `REPORT.md` disagree, the event log wins and you have a bug to flag —
  do not silently pick the prettier number.
- **Record the dead ends.** The **试了但没过** table is mandatory when there were
  non-accepts. A round that only lists wins is hiding the cost of the search (mirror the
  reference doc's "Vec→字段零变化 → 方向关闭").
- **`accepted` ≠ should-merge.** Report a `relaxed` win as a human decision, never as a
  merged improvement. The judge weighed correctness+speed, not architecture.
- **No padded directions.** §三 lists only what the end-state supports; "drained" is a
  valid, honest conclusion.

## Output + optional Lark push

- Write the filled report to `DIR/DAILY-REPORT.md`.
- **Always render a PNG and embed THAT, not the `.svg`** — markdown previewers don't render
  SVG, so `![](trajectory.svg)` shows nothing. Do:
  `qlmanage -t -s 1100 -o DIR DIR/trajectory.svg && mv DIR/trajectory.svg.png DIR/trajectory.png`
  (macOS), then `![trajectory](trajectory.png)` in the report.
- To publish to Lark, switch to the `lark-doc` skill: `docs +create --api-version v2`
  with the markdown, then `docs +media-insert` the PNG. **Publishing is an outward action
  — confirm with the user first** (and per global rules, never post without approval).
