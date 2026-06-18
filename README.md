# ARO (Python) — Auto-Research Optimizer

An autonomous, goal-driven optimization loop for code. **stdlib-only, zero external deps.**

The point of the loop: **generation is commodity (a `claude` write-compile-fix
loop), the engineering weight is the judge** — a deterministic evaluator that
can't be fooled or gamed on a sub-1% change buried in noise. ARO profiles the
real hot path, reads it to form a plan, implements ONE behaviour-preserving
change, and only believes a win it can prove.

Generality is via a **spec, not code**: a new target is one JSON file in
`targets/`; the loop, judge, and generator never change.

## Layout

| path | role |
|---|---|
| `aro/types.py` | core types (Candidate, Patch, Metrics, Objective, MetricDelta, Verdict, NoiseFloors, **Report**) |
| `aro/spec.py` | load a `targets/*.json` TargetSpec (+ Goal / Stop) |
| `aro/target.py` | `SpecTarget`: the generic driver — git-worktree isolation, build/test/bench/differential, region hint |
| `aro/profile.py` | the **observe arm**: macOS `sample` CPU profiler → ranked in-binary hot functions |
| `aro/context.py` | code-context provider: pulls the spec's anchors (struct/fn) in front of the generator |
| `aro/guard.py` | reward-hacking screen (deps / bench / tests / path-escape are off-limits) |
| `aro/stats.py` | median, quantile, seeded bootstrap CI |
| `aro/eval.py` | the **评判器**: A/A floor calibration, paired A/B, bootstrap CI, the two gates |
| `aro/store.py` | **memory**: append-only `records.jsonl` + pareto + floors (resumable) |
| `aro/generator.py` | generation: `PlannedGenerator` (seeded) / `RalphGenerator` (thin live, one-shot `claude -p`) / `AgenticGenerator` (heavy live, write-compile-fix + read + reflect) — the spec's `generator` slot picks |
| `aro/engine.py` | the loop: freeze baseline → calibrate → read → generate → judge → record; **compounds accepted patches into the baseline** |
| `aro/events.py` | structured event log (`events.jsonl`) — the machine-readable source of truth |
| `aro/prompts.py` | loads the executed prompt templates from `skill/prompts/*.md` |
| `aro/__main__.py` | the CLI (`python3 -m aro run <spec>`) |
| `targets/*.json` | one declarative spec per target |
| `probes/*.rs` | microbench probes the driver drops into a worktree as a cargo `example` |
| `find_hotpath.py` | observe only: profile + isolated-kernel latency, no changes |
| `verify_patch.py` | re-score a recorded patch through the full judge |
| `selftest.py` | cargo-free mock-target test for compounding + event log |
| `ralph.sh` | the pure-shell Ralph Loop (generation only — pipe its patches into the judge); `RalphGenerator` is its in-loop Python equivalent (`generator: "ralph"`) |
| `skill/SKILL.md` + `skill/references/` | the committable skill (prose docs) |
| `skill/prompts/` | the executed prompt templates (read / agentic / guided + blind hint) |

## The two gates (评判器)

0. **Guard**: the patch may touch only implementation source. `Cargo.toml`/`Cargo.lock`,
   `benches/`, `tests/`, and any path escape are rejected before a build — that is
   how "swap in a library" or "edit the ruler" gets stopped.
1. **Correctness**: candidate is a patch on a *frozen* baseline worktree →
   `cargo build --release` → `cargo test --release` → differential vs baseline.
   Any failure → discard.
2. **Significance**: paired A/B bench (interleaved, drift-cancelling) → per-metric
   Δ% with a bootstrap CI, checked against an **A/A-calibrated noise floor**. A
   change counts only if it clears the floor *and* its CI excludes 0.

## Run

```sh
cd aro
python3 -m aro run targets/<name>.json --rounds 1
#   --blind     profiler-only hint (no technique spelled out) — honest discovery
#   --aa-runs N --ab-pairs N   measurement power   |   --no-read   skip the read phase
```

Worktrees are created from the frozen baseline under the target repo's `.aro-worktrees/` and
removed after each candidate; each worktree gets its OWN `CARGO_TARGET_DIR`
(`.aro-<name>-td/<worktree>`) — a shared one makes cargo reuse the first worktree's
build for the others, so baseline and candidate would compare the same binary
(Δ and differential meaningless); the cost is recompiling per candidate. The run's
truth lands in `--out/events.jsonl`.

## The observe arm (profiling)

The design calls for an *observe* step that tells the generator **where the work
is** — not just "make this number smaller":

- `aro/profile.py` runs the baseline under macOS `/usr/bin/sample` (no sudo),
  demangles Rust symbols, and ranks the heaviest in-binary functions — surfacing
  the real hot kernel (often ~70%+ of the time) rather than a readable-but-cold
  path that's tempting to tune first.
- The hot region + the spec's context anchors feed `GenContext.region_hint`, so
  the generator is told the measured hotspot and the code around it.
- The metric is isolated by a `probes/*.rs` microbench (a kernel that is most of
  an end-to-end number is still *diluted* there); only a direct microbench makes
  it cleanly optimizable and measurable.

## Compounding & the event feed

- **Compounding (越跑越好)**: when a candidate is accepted, its patch is folded
  into the working baseline and rebuilt, so the next round is *generated and
  measured on top of it* — improvements stack across rounds instead of every
  candidate racing the original baseline.
- **Event feed (`events.jsonl`)**: every step appends one flushed JSON line
  (`run_started`, `baseline_built`, `floors_calibrated`, `round_started`,
  `read_phase`, `candidate_proposed`, `gate`, `candidate_verdict`,
  `baseline_advanced`, `run_finished`), with floors and per-candidate deltas
  written in full. It is the live progress feed (`tail -f`) **and** the source
  the report is rendered from.

## The report (skill-rendered, not coded)

There is no `report.py`. `RUN-REPORT.md` is rendered from `events.jsonl` by the
`aro` skill's report flow (`skill/references/report-protocol.md`), with every
number (Δ/CI/floor/verdict) copied verbatim and verdicts never re-judged — so
report prose stays out of code and a within-noise result can't be laundered into
a win.

## Status

- Spec-driven loop, judge, memory, generic `SpecTarget`, guard, observe arm
  (profiler), read phase, agentic generator, compounding, event log:
  implemented. `selftest.py` proves compounding + events without a cargo build.
- On a real run, the agentic generator autonomously derived a multi-site,
  behaviour-preserving optimization that, under per-worktree isolation, verified as
  a **~14% speedup** (Δ well clear of the noise floor, random-input differential
  byte-identical, accepted). A separate blind run confidently shipped a **−53%
  regression** that only the sound judge caught. Both numbers were once masked as
  within-noise by a shared-target-dir bug (baseline and candidate compiled to the
  same binary) — fixed by per-worktree dirs. The lessons live in `memory/lessons.jsonl`.
- Differential: when the spec names a `differential` probe, ARO runs the same
  deterministic random-input probe in baseline + candidate and requires identical
  output — a real byte-identical check; clean-tree MVP otherwise.
