# ARO — Auto-Research Optimizer

**An autonomous optimization loop for performance-critical code.** ARO profiles the
real hot path, makes **one** behaviour-preserving change, and **only believes a win
it can prove.** Pure-stdlib Python, zero dependencies.

> **The loop is commodity; the judge is the moat.**
>
> Generating a candidate optimization is something any coding agent can do. The hard,
> valuable part — the part most "AI optimizers" skip — is a *deterministic evaluator
> that can't be fooled or gamed* on a sub-1% change buried in benchmark noise. On
> consensus / crypto / EVM code a faster-but-wrong change is a disaster, so behaviour
> must stay byte-identical; and real wins are often smaller than the run-to-run noise.
> ARO puts the engineering weight there.

---

## Picking this up as an AI agent

Two entry points, by what you're doing:

- **Consuming a finished run** (e.g. turn its wins into a PR) — run
  `python3 -m aro manifest <out-dir>`. `manifest.json` is the final accepted edit-set with
  full provenance (attempt · id · fn · files · Δ · regime · critic verdict) and a
  **`mergeable`** flag. Apply the patches in `order` on `baseline_ref`; **`accepted` ≠
  should-merge** — only `mergeable:true` (byte-identical + clean critic) is safe to PR
  directly, the rest need a human call. The full data contract — every file/field, the
  `events.jsonl` schema, the attempt/id linkage, the `base-*` skip rule — is
  [`skill/references/run-data.md`](skill/references/run-data.md).
- **Operating ARO** (run or extend it) — [`skill/SKILL.md`](skill/SKILL.md) is the
  operator's index: every subcommand + a routing table into the protocol docs under
  `skill/references/`.

A run's **source of truth is its `events.jsonl`** (append-only, one line per step);
everything else (`manifest.json`, `decision-tree.html`, `REPORT.md`, the charts) is derived
from it and regenerable with `aro tree` / `aro manifest` — no re-run, no cost.

---

## What the judge catches

The point of the judge is that the generator can't be trusted on its own:

- The agentic generator has derived a multi-site, behaviour-preserving optimization that
  verified as a **+14%** win — Δ well clear of the noise floor, random-input differential
  byte-identical, accepted.
- A separate run confidently produced a **−53% regression** that *only the judge caught*:
  the change was byte-identical and passed every test, but the paired A/B + CI showed it
  was slower, not faster.
- Both were once masked as "within-noise" by a shared-build-dir bug (baseline and candidate
  compiled to the same binary) — surfaced and fixed by per-worktree build dirs.

These accumulate in [`memory/lessons.jsonl`](memory/lessons.jsonl) and feed back into later
runs, so the loop doesn't repeat a known dead end. The blind agent also has a real failure
mode worth stating plainly: a generic prompt tends to find the right hot path but stop at a
*safe, local* tweak the judge rejects as within-noise — reaching the deeper algorithmic win
takes prompting that asks "is this work even necessary?", not just "make it faster".

---

## How it works

```
observe → read → generate → judge → record → reflect → (goal met / dry? → stop)
                                ▲                                   │
                                └──────── compound + next round ────┘
```

- **observe** — a real CPU profiler (macOS `sample`, no sudo; Linux `perf`) ranks the
  heaviest in-binary functions, so the generator optimizes the *measured* hot path, not
  readable-but-cold code that's tempting to tune first.
- **read** — a read-only analysis turns the hot function and the data it touches into a
  concrete plan for one byte-identical change. (A prompt *lens* pushes it from "make this
  faster" toward "is this work even necessary?" — the question that finds algorithmic wins.)
- **generate** — a write-compile-fix loop in a throwaway git worktree
  (`edit → build → test → fix → …`) produces a candidate diff.
- **judge** — the deterministic gates below score it. **This is the moat.**
- **record / reflect** — accepted patches compound into the working baseline (so the next
  round is measured *on top of* them); every verdict feeds a forward-looking research
  agenda; cross-run lessons persist.

---

## The judge (the moat)

Three gates, in order; any failure short-circuits. Code: `aro/eval.py`, `aro/stats.py`,
`aro/guard.py`.

**Gate 0 — reward-hacking guard.** A path-only screen, before any build. Reject patches
that touch `Cargo.toml`/`Cargo.lock` (swap in a library), `benches/` or `tests/` (the
ruler and the judge), escape the worktree, or edit outside the spec's declared regions.
Cheap, language-agnostic, impossible to argue with.

**Gate 1 — correctness (before any speed).** On a *frozen* baseline worktree: apply →
build (**+ a recompile self-check**) → test (must keep the baseline's passing-test count)
→ **differential** — feed many deterministic pseudo-random inputs through the hot function
in both baseline and candidate and require **byte-identical** output. Any failure → discarded.

**Gate 2 — significance (only if correct).** Paired, order-alternated A/B benchmarking →
per-metric Δ% with a seeded **bootstrap CI**, checked against an **A/A-calibrated noise
floor**. A change is `accepted` only if it clears the floor **and** its CI excludes 0 —
killing the two classic false positives: machine drift (cancelled by the alternated
pairing) and a lucky single sample (the CI must agree on the sign). Direction-aware per
objective.

The verdict is only as sound as the binaries it benches, so the judge self-checks:
**per-worktree `CARGO_TARGET_DIR`** (a shared one makes cargo reuse the first worktree's
build, collapsing every Δ to ≈0) and a **forced recompile** of the edited crate
(`cargo clean -p <pkg>` before the build, so a changed candidate can't bench a stale
binary; with the `Compiling`-line check as a fallback when a scoped clean can't run).

---

## Quickstart

Pure-stdlib Python (3.9+). Drives Rust targets today, via `cargo`.

```sh
git clone https://github.com/RealiCZ/aro && cd aro
python3 selftest.py        # cargo-free sanity check (compounding + event log)
```

**Spec-driven** — when you've already isolated the metric. A new target is **a spec, not
code**: one JSON file (`targets/<name>.json`) authored as **7 slots** — `target_repo`,
`hot_path`, `metric`, `direction`, `benchmark_probe`, `correctness_oracle`, `constraints`
(+ a `run` block of loop knobs). The loop, judge, and generator never change.

```sh
# turn a free-form goal into a validated spec (detect → fill slots + write probes → dry-run)
python3 -m aro plan "make the scalar-mul faster" /path/to/repo
#   → prints a slot dump + dry-run results, writes targets/<name>.json

# or copy examples/target.example.json, fill the slots, then run it:
python3 -m aro run targets/<name>.json --rounds 3
#   --blind                    profiler-only hint (no technique named) — honest discovery
#   --aa-runs N --ab-pairs N   measurement power
#   --out DIR                  where events.jsonl lands
```

**Autonomous** — no spec yet. The agent profiles, writes its own probe, optimizes, and
verifies, unattended. See
[`skill/references/autonomous-optimization.md`](skill/references/autonomous-optimization.md).

**Unattended / whole-frontier (L3)** — walk the profiled hot frontier, judge each function,
compound the wins, re-profile on top, until the frontier or the attempt budget is spent:

```sh
python3 -m aro sweep targets/<name>.json                       # L1: the frontier map (report-only)
python3 -m aro sweep targets/<name>.json --attempt --diverge --critic
#   --critic        second judge (independent semantic reviewer) — catches reward-hacks / gamed benches
#   --out-dir DIR   compounding wins land here; re-point to the same DIR to RESUME from the advanced baseline
```

**Report & hand-off** — derived from a run's `events.jsonl` (no re-run, no cost):

```sh
python3 -m aro tree <out-dir>                    # (re)render the exhaustion-ledger report + tree.json
python3 -m aro manifest <out-dir>                # final accepted edit-set → manifest.json (run → PR)
python3 -m aro serve <out-dir> --port 8010       # serve the report over HTTP, live-refreshing (server runs)
```

Both modes write the run's machine-readable truth to `events.jsonl` — a live `tail -f`
feed **and** the source the human report is rendered *from* (numbers copied verbatim,
verdicts never re-judged, so a within-noise result can't be laundered into a win).

Worktrees are created from the frozen baseline under the target repo's `.aro-worktrees/`
and removed after each candidate; each gets its own `CARGO_TARGET_DIR` (the cost is
recompiling per candidate — the price of a sound measurement).

---

## Generators (the commodity part)

The spec's `generator` slot picks how candidates are produced — the judge is identical
either way:

- **`agentic`** (default) — a heavy live `claude` write-compile-fix loop with read +
  reflect; unlocks multi-site refactors a one-shot patch can't reach.
- **`ralph`** — a thin one-shot `claude -p` → a block patch.
- **`PlannedGenerator`** — a seeded edit, used by `verify_patch.py` to re-score a recorded
  patch deterministically through the full judge.

---

## What it won't do (honest)

- **It can't resolve a change below the noise floor.** A real sub-floor win measures
  `within-noise`. Raise `--aa-runs`/`--ab-pairs`, or accept the gain isn't provable here —
  never lower the bar.
- **The generator is a model (non-deterministic); only the judge is code.** Re-runs
  propose different patches; reproducibility lives in the judge + seeded statistics.
- **The metric must be isolable** behind a microbench — a kernel diluted in an end-to-end
  number can't be optimized measurably.
- **Single-machine measurement.** Paired, order-alternated A/B cancels slow drift, not a
  busy box. Run on a quiet machine; treat one round as weak evidence — the value is
  multi-round (compounding + the agenda).

---

## Layout

| path | role |
|---|---|
| `aro/engine.py` | the loop (`RunConfig` + phase methods): freeze → resume → calibrate → generate → prescreen → judge → fold → reflect; **compounds accepted patches into the baseline** |
| `aro/eval.py` | the judge: A/A floor calibration, paired A/B, bootstrap CI, the three gates, prescreen (+ worktree hand-off to the judge — no double build) |
| `aro/guard.py` · `aro/stats.py` | reward-hacking screen · median/quantile/seeded bootstrap CI |
| `aro/target.py` | `SpecTarget`: the generic driver — git-worktree isolation, build/test/bench/differential (public surface: `td_for`/`env_for`/`pkg_dir`/`write_probe`/`run_diff_probe`) |
| `aro/profile.py` · `aro/symbols.py` | cross-platform CPU profiler (macOS `sample` / Linux `perf`) · v0 demangling + owner classification |
| `aro/frontier.py` | workspace ownership, hot-fn bucketing, headroom arithmetic, the explorer's stop rule |
| `aro/attempt.py` | the **L3 meta-loop** (`aro sweep --attempt`) + the **L4a probe rescue** + the **L4b multi-workload campaign** + finalize |
| `aro/sweep.py` | the L1 frontier MAP (report-only) + profiling entry |
| `aro/probe_factory.py` | **L4a**: agent-authored isolation micro-benches behind a probe-judge (A/A gain · relevance · scale-aware · frozen-before-generate) + the parent-oracle mutation coverage check |
| `aro/workload_factory.py` | **L4b**: agent-authored workload variants behind a workload-judge (determinism · oracle mutation test · coverage increment · frozen) — wins tagged `synthetic-workload`, never auto-mergeable |
| `aro/permtree.py` | **L4c**: the permanent decision tree (`memory/permtree/`) — cross-run node ledger + the three-boundary exhaustion proof |
| `aro/generator.py` | `agentic` / `ralph` / `PlannedGenerator` — the spec's `generator` slot picks |
| `aro/critic.py` | the **second judge**: independent adversarial semantic reviewer (`--critic`) |
| `aro/llm.py` · `aro/vcs.py` | the single `claude` invocation point (timeouts, `ARO_CLAUDE_BIN`) · git plumbing with timeouts everywhere |
| `aro/runlog.py` · `aro/events.py` | the single events.jsonl READER (latest-run slice) · the structured event WRITER (source of truth) |
| `aro/patchfile.py` · `aro/store.py` | the SEARCH/REPLACE patch-format owner · memory: records + pareto + floors (resumable) |
| `aro/spec.py` · `aro/types.py` | declarative `targets/*.json` loader (validated at load) · core types + the one headline-Δ rule |
| `aro/manifest.py` · `aro/tree.py` · `aro/chart.py` | run → PR hand-off (`mergeable` flag) · the exhaustion-ledger report (`aro/ledger_template.html`, no build step) · SVG figures |
| `aro/cli.py` · `aro/serve.py` · `aro/verify.py` | the one argparse CLI surface · live HTTP report (127.0.0.1 default) · re-score a recorded patch |
| `aro/plan.py` · `aro/context.py` · `aro/prompts.py` | free-form goal → validated spec (`aro plan`) · code-context provider · prompt-template loader |
| `targets/*.json` · `probes/*.rs` · `fixtures/mini-target/` | specs · microbench probes · the cargo E2E fixture crate |
| `tests/e2e_fixture.py` · `selftest.py` | the REAL-judge E2E (worktree→build→test→differential→A/B→manifest, + probe qualification) · 22 isolated cargo-free case groups |
| `memory/lessons.jsonl` · `memory/permtree/` | cross-run lessons · the permanent decision-tree ledger |
| `skill/` | the committable skill — prose docs (`references/`) + the executed prompt templates (`prompts/`) |

---|---|
| `aro/engine.py` | the loop: freeze baseline → calibrate → read → generate → judge → record; **compounds accepted patches into the baseline** |
| `aro/eval.py` | the judge: A/A floor calibration, paired A/B, bootstrap CI, the three gates |
| `aro/guard.py` | reward-hacking screen (deps / bench / tests / path-escape / out-of-region are off-limits) |
| `aro/stats.py` | median, quantile, seeded bootstrap CI |
| `aro/target.py` | `SpecTarget`: the generic driver — git-worktree isolation, build/test/bench/differential, region hint |
| `aro/profile.py` | the **observe arm**: cross-platform CPU profiler (macOS `sample` / Linux `perf`) → ranked in-binary hot functions |
| `aro/generator.py` | `agentic` / `ralph` / `PlannedGenerator` — the spec's `generator` slot picks |
| `aro/critic.py` | the **second judge**: an independent, adversarial semantic reviewer (`--critic`) — catches reward-hacks / gamed benches / known-bad patterns the deterministic gates can't |
| `aro/sweep.py` | the **L3 meta-loop**: profile → bucket ours/untouchable → walk the hot frontier → judge each fn → compound → re-profile (`aro sweep --attempt`) |
| `aro/plan.py` | free-form goal → validated 7-slot spec (an agent writes the probe + differential in a throwaway worktree, then a dry-run) (`aro plan`) |
| `aro/store.py` | memory: append-only records + pareto + calibrated floors (resumable) |
| `aro/events.py` | structured event log (`events.jsonl`) — the machine-readable **source of truth**; stamps the `attempt` index onto each event |
| `aro/manifest.py` | the **hand-off**: reconstruct a run's final accepted edit-set + provenance + `mergeable` flag → `manifest.json` (`aro manifest`) |
| `aro/tree.py` · `aro/chart.py` · `aro/trajectory.py` | render the run report from `events.jsonl` — `decision-tree.html` + the perf/trajectory charts (`aro tree`) |
| `aro/serve.py` | serve a run's report over HTTP, live-refreshing from `events.jsonl` (`aro serve`, for headless server runs) |
| `aro/lessons.py` | cross-run lessons: recall prior verdicts to skip dead ends, append new ones |
| `viz/` | the Svelte front-end for the report, built into `aro/decision_tree_template.html` (Python injects the run's data; no re-build needed to view) |
| `aro/spec.py` · `aro/types.py` | declarative `targets/*.json` loader · core types |
| `aro/context.py` · `aro/prompts.py` | code-context provider · loader for the executed prompt templates |
| `aro/__main__.py` | the CLI (`python3 -m aro run <spec>`) |
| `targets/*.json` · `probes/*.rs` | one declarative spec per target · microbench probes dropped into a worktree as a cargo `example` |
| `find_hotpath.py` · `verify_patch.py` | observe-only profiling · re-score a recorded patch through the full judge |
| `selftest.py` | cargo-free mock-target test for compounding + event log |
| `memory/lessons.jsonl` | cross-run memory of wins and dead ends, fed back into later runs |
| `skill/` | the committable skill — prose docs (`references/`) + the executed prompt templates (`prompts/`) |

---

ARO is inspired by Karpathy's [autoresearch](https://github.com/karpathy/autoresearch),
hardened for code where *correctness is non-negotiable*: it finds where the time really
goes, changes it, and believes only a win it can prove.
