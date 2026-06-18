---
name: aro
version: 0.1.0
description: Autonomous, goal-driven performance optimization for a code repo. Profile the real hot path, read it to form a plan, implement ONE behaviour-preserving change with an agentic write-compile-fix loop, and score it with a trustworthy statistical judge (A/A noise floor + paired A/B + bootstrap CI) — repeat until the goal is met or returns dry. Use to auto-optimize a Rust crate's performance without breaking behaviour.
---

# ARO — autonomous performance-optimization loop

Inspired by Karpathy's autoresearch, hardened for code where *correctness is non-negotiable*: the loop is commodity; the value is a judge that can't be fooled or gamed. ARO finds where the time really goes, changes it, and only believes a win it can prove.

## Subcommands

| command | purpose |
|---|---|
| `python3 -m aro run <spec.json>` | run the full loop on a target spec |
| `python3 -m aro run <spec.json> --blind` | same, profiler-only hint (no technique spelled out) — honest blind-discovery mode |
| `python3 find_hotpath.py` | observe only: profile + isolated-kernel latency, no changes |
| `python3 verify_patch.py <patch> --spec <spec.json>` | re-score a recorded patch through the full judge |
| `python3 selftest.py` | cargo-free self-test (compounding + event log) |
| _(skill flow, no script)_ | render `RUN-REPORT.md` from a run's `events.jsonl` — numbers verbatim (`references/report-protocol.md`) |
| _(skill flow, no script)_ | plan a new target → validated `targets/<name>.json`, dry-running build+probe+test (`references/plan-workflow.md`) |

## When to activate

- "make `<crate>` faster without changing behaviour"
- "find and fix the real performance bottleneck in this repo"
- "reproduce / verify a perf optimization, and prove it isn't noise"
- any over-night, self-verifying optimization run on consensus / crypto / EVM code
- "add a target / set up ARO on this repo", "what metric should I use" → run the plan workflow (`references/plan-workflow.md`)
- a repo with NO spec yet, run it fully unattended (the agent profiles + writes its own probe + verifies) → `references/autonomous-optimization.md`

## Setup phase (per target, once)

Fill these slots by hand or — better — via the **plan workflow**
(`references/plan-workflow.md`), which detects build/test, writes a probe, and
**dry-runs build+probe+test** before emitting the spec.

1. **Pick the target** — point at the repo and the frozen baseline (`repo`, `baseline_ref`).
2. **Declare build & test** — the commands that compile and prove correctness.
3. **Make the hot metric measurable** — a `probes/<x>.rs` microbench that isolates the kernel and prints `ARO_..._SAMPLES <ns...>`. If the highest-leverage op has no benchmark, *write one* — it cannot be optimized while diluted in an end-to-end number.
4. **Name the editable regions + context anchors** — which files may change, and which `(struct/fn)` to put in front of the generator.
5. **Set objectives + the goal + the stop** — metric, direction, optional target value; `max_rounds` and `dry_rounds`.
6. **Wire the prompts** — `prompts/*.md` (the executed templates: read / agentic / guided + blind hint).
7. Write it all into one `targets/<name>.json` (schema: `references/spec-slots.md`). New repo = new spec, no new code.

## The loop (per round)

```
observe  : profile the baseline → hottest function → region hint
read     : READ-ONLY analysis → a precise plan (what to change, why byte-identical, what layout)   [prompts/read.md]
generate : (default "agentic") write-compile-fix in a throwaway worktree → edit→build→test→fix→… → take the diff;  "ralph" = one read-only claude -p → a block patch (thin)   [prompts/agentic.md · prompts/ralph.md]
judge    : guard → build → test → differential → paired A/B vs A/A floor + bootstrap CI → verdict   [references/judge-protocol.md]
record   : write result to memory; an accepted patch compounds into the working baseline
reflect  : distil this round's verdicts into forward-looking research directions (the agenda) — exploit a win's variants, combine near-misses, change layout, raise power   [prompts/reflect.md]
check    : goal met? dry for K rounds? cap hit? → stop, else next round
report   : (on finish) render RUN-REPORT.md FROM events.jsonl — numbers verbatim, verdicts never re-judged   [references/report-protocol.md]
```

## Critical rules

1. **The writer never grades itself.** A separate, deterministic evaluator scores every candidate.
2. **"Looks faster" is banned.** A win counts only if the measured Δ clears the A/A noise floor AND a bootstrap CI that excludes 0.
3. **Behaviour stays byte-identical.** Correctness gate (build + test + differential vs frozen baseline) runs before significance; any failure discards the candidate.
4. **One behaviour-preserving change per round.**
5. **Read before write.** Derive a plan read-only before implementing.
6. **Profile, don't guess.** Optimize the measured hot path, never the code that's easy to read.
7. **Edit only implementation source.** Never `Cargo.toml`/`Cargo.lock`, `benches/`, `tests/` (the ruler and the judge) — a patch touching them is auto-rejected.
8. **Add no dependencies; don't swap in a library.**
9. **Memory is durable, and forward-looking.** Every result is recorded and the next round reads it; accepted patches fold into the baseline so gains compound; the reflect step distils each round's verdicts into an **agenda** of directions to try next — so the loop accumulates direction, not just a list of dead ends.
10. **Stop on the goal, not the clock.** Stop when the target is met or after `dry_rounds` consecutive non-accepts — no fixed run length, no work-cap timeout on the agent (only a high hang-guard).
11. **The judge is code; the rest is prompt.** Never reason out a verdict — statistics must be reproducible and ungameable. That executed core (`aro/{eval,stats,guard}.py`) is the moat.
12. **The report is a view of the event log, never a re-judgement.** `RUN-REPORT.md` is rendered from `events.jsonl` with every number (Δ/CI/floor/verdict) copied verbatim; a within-noise or regressed result is never written up as a win. The report cannot launder a verdict.

## Principles reference

The deeper "why" behind the rules lives in `references/core-principles.md`; the loop phases in `references/autonomous-loop-protocol.md`; the judge in `references/judge-protocol.md`; the persisted state schema in `references/results-logging.md`; the target spec in `references/spec-slots.md`; how the report is rendered from the event log in `references/report-protocol.md`; the new-target wizard in `references/plan-workflow.md`; the unattended "agent writes its own probe" flow in `references/autonomous-optimization.md`.

Two kinds of file, two folders: `references/*.md` are **prose docs** you read to understand the system; `prompts/*.md` are the **executed templates** (`$placeholder` substitution) that ARO actually feeds the model — `aro/prompts.py` loads them, a spec's `prompts` slot names them.

## Domain adaptability

Same loop, different spec — only the metric, probe, and regions change:

| target | metric | hot path | correctness gate |
|---|---|---|---|
| salt committer | `mul_index` ns | EC fixed-base scalar mult (banderwagon) | `cargo test -p banderwagon` (mul_index vs reference MSM) |
| salt trie | update ns / allocs | trie update/finalize | salt test suite + byte-identical root |
| mega-evm | per-opcode latency | opcode hot path | differential vs frozen + regression tests |
| generic service | p95 latency | request handler / I/O | regression suite passes |

## Limitations (honest)

ARO is not magic; state what it cannot do (autoresearch principle 7):

- **It can't resolve a change below the noise floor.** If the A/A floor is high (e.g. 5.45% on a noisy kernel with few A/B pairs), a real sub-floor win measures within-noise. Raise `aa_runs`/`ab_pairs`, or accept the gain isn't provable here — never lower the bar.
- **`differential` is a test-suite-backed MVP, not random-input fuzz.** Byte-identical behaviour is only as strong as the test coverage; true differential fuzz (a `probes/` Rust target) is a TODO. Don't claim a guarantee the tests don't give.
- **Measurement is single-machine.** Paired, order-alternated A/B cancels slow drift, not a busy machine. Run on a quiet box; treat one round as weak evidence.
- **The generator is a model (non-deterministic); only the judge is code.** Re-runs propose different patches — reproducibility lives in the judge + seeded stats, not the generation.
- **The metric must be isolable.** If the highest-leverage operation can't be put behind a microbench probe, ARO can't optimize it measurably.
- **Single-round hit rate is low by design.** Most candidates are sub-noise; value is multi-round (compounding + the agenda). Judge over days, not one round.
