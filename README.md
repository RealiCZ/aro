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

- **observe** — a real CPU profiler (macOS `sample`, no sudo) ranks the heaviest
  in-binary functions, so the generator optimizes the *measured* hot path, not
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
build, collapsing every Δ to ≈0) and the **recompile check** (a changed candidate whose
build emitted no `Compiling` line reused a stale binary → rejected as measurement-unsound).

---

## Quickstart

Pure-stdlib Python (3.9+). Drives Rust targets today, via `cargo`.

```sh
git clone https://github.com/RealiCZ/aro && cd aro
python3 selftest.py        # cargo-free sanity check (compounding + event log)
```

**Spec-driven** — when you've already isolated the metric. A new target is **a spec, not
code**: one JSON file (`targets/<name>.json` — build/test/bench/regions/objectives + goal
+ stop). The loop, judge, and generator never change.

```sh
python3 -m aro run targets/<name>.json --rounds 3
#   --blind                    profiler-only hint (no technique named) — honest discovery
#   --aa-runs N --ab-pairs N   measurement power
#   --out DIR                  where events.jsonl lands
```

**Autonomous** — no spec yet. The agent profiles, writes its own probe, optimizes, and
verifies, unattended. See
[`skill/references/autonomous-optimization.md`](skill/references/autonomous-optimization.md).

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
| `aro/engine.py` | the loop: freeze baseline → calibrate → read → generate → judge → record; **compounds accepted patches into the baseline** |
| `aro/eval.py` | the judge: A/A floor calibration, paired A/B, bootstrap CI, the three gates |
| `aro/guard.py` | reward-hacking screen (deps / bench / tests / path-escape / out-of-region are off-limits) |
| `aro/stats.py` | median, quantile, seeded bootstrap CI |
| `aro/target.py` | `SpecTarget`: the generic driver — git-worktree isolation, build/test/bench/differential, region hint |
| `aro/profile.py` | the **observe arm**: macOS `sample` CPU profiler → ranked in-binary hot functions |
| `aro/generator.py` | `agentic` / `ralph` / `PlannedGenerator` — the spec's `generator` slot picks |
| `aro/store.py` | memory: append-only records + pareto + calibrated floors (resumable) |
| `aro/events.py` | structured event log (`events.jsonl`) — the machine-readable source of truth |
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
