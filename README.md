# ARO: Auto-Research Optimizer

**An autonomous optimization loop for performance-critical code.** ARO profiles the
real hot path, makes one behaviour-preserving change at a time, and only believes a
win it can prove. Pure-stdlib Python, zero runtime dependencies. It drives Rust
targets today, through cargo.

> **The loop is commodity; the judge is the moat.**
>
> Any coding agent can generate a candidate optimization. The hard part, the part
> most "AI optimizers" skip, is a deterministic evaluator that cannot be fooled on a
> sub-1% change buried in benchmark noise. On consensus / crypto / EVM code a
> faster-but-wrong change is a disaster, so behaviour must stay byte-identical, and
> real wins are often smaller than run-to-run noise. ARO puts the engineering weight
> there.

---

## Picking this up as an AI agent

Two entry points, depending on what you are doing:

- **Consuming a finished run** (for example, turning its wins into a PR): run
  `python3 -m aro manifest <out-dir>`. `manifest.json` is the final accepted edit set
  with full provenance (attempt, id, fn, files, delta, regime, critic verdict) and a
  `mergeable` flag. Apply the patches in `order` on `baseline_ref`. Note that
  `accepted` does not mean should-merge: only `mergeable:true` entries
  (byte-identical regime plus a clean critic pass) are safe to PR directly; the rest
  need a human call. The full data contract lives in
  [`skill/references/run-data.md`](skill/references/run-data.md).
- **Operating ARO** (running or extending it): [`skill/SKILL.md`](skill/SKILL.md) is
  the operator's index, with every subcommand and a routing table into the protocol
  docs under `skill/references/`.

A run's source of truth is its `events.jsonl` (append-only, one line per step).
Everything else (`manifest.json`, the report, the charts) is derived from it and can
be regenerated with `aro tree` / `aro manifest` at no cost.

---

## What the judge catches

The generator cannot be trusted on its own. Real examples from this repo's runs:

- The agentic generator derived a multi-site, behaviour-preserving optimization that
  verified as a **+14% win**: delta well clear of the noise floor, random-input
  differential byte-identical, accepted.
- A separate run confidently produced a **-53% regression** that only the judge
  caught: the change was byte-identical and passed every test, but the paired A/B
  with CI showed it was slower.
- Both were once masked as within-noise by a shared-build-dir bug (baseline and
  candidate compiled to the same binary). Per-worktree build dirs surfaced and fixed
  it.

Lessons like these accumulate in [`memory/lessons.jsonl`](memory/lessons.jsonl) and
feed back into later runs, so the loop does not repeat a known dead end.

---

## How it works

```
observe -> read -> generate -> judge -> record -> reflect -> (goal met / dry? -> stop)
                                 ^                                   |
                                 +-------- compound + next round ----+
```

- **observe**: a real CPU profiler (macOS `sample`, no sudo; Linux `perf`) ranks the
  heaviest in-binary functions, so the generator works on the measured hot path
  instead of readable-but-cold code.
- **read**: a read-only analysis turns the hot function and the data it touches into
  a concrete plan for one byte-identical change.
- **generate**: a write-compile-fix loop in a throwaway git worktree produces a
  candidate diff.
- **judge**: the deterministic gates below score it. This is the moat.
- **record / reflect**: accepted patches compound into the working baseline, so the
  next round is measured on top of them. Every verdict feeds a forward-looking
  research agenda, and cross-run lessons persist.

---

## The judge

Three gates, in order; any failure short-circuits. Code: `aro/eval.py`,
`aro/stats.py`, `aro/guard.py`.

**Gate 0, the reward-hacking guard.** A path-only screen before any build. Reject
patches that touch `Cargo.toml`/`Cargo.lock` (swapping in a library), `benches/` or
`tests/` (the ruler and the judge), escape the worktree, or edit outside the spec's
declared regions. Cheap, language-agnostic, impossible to argue with.

**Gate 1, correctness before speed.** On a frozen baseline worktree: apply, build,
test (the candidate must keep the baseline's passing-test count), then the
**differential**: feed many deterministic pseudo-random inputs through the hot
function in both baseline and candidate and require byte-identical output. Any
failure discards the candidate.

**Gate 2, significance.** Paired, order-alternated A/B benchmarking gives a
per-metric delta with a seeded bootstrap CI, checked against an A/A-calibrated noise
floor. A change is accepted only if it clears the floor and its CI excludes zero.
That kills the two classic false positives: machine drift (cancelled by the
alternated pairing) and a lucky single sample (the CI must agree on the sign).

The verdict is only as sound as the binaries it benches, so the judge self-checks:
each worktree gets its own `CARGO_TARGET_DIR` (a shared one makes cargo reuse the
first worktree's build, collapsing every delta to about zero), and the edited crate
is force-recompiled before benching so a changed candidate can never bench a stale
binary. Prescreen survivors hand their already-built worktree to the judge, so
nothing is compiled twice.

A second, independent judge is available with `--critic`: an adversarial semantic
review that catches reward hacks, gamed benches, and known-bad patterns the
deterministic gates cannot see. Two judges, AND not OR.

---

## The self-extending layer

When the search runs out of road, ARO can grow its own measurement tools and its own
workloads. Every self-made tool passes a deterministic qualification gate before it
is allowed to judge anything, and it is frozen (sha256 recorded) before any
candidate generation, so a tool can never be tuned to flatter a specific patch.

- **Probe factory** (`aro/probe_factory.py`, on by default under `--diverge`): a
  noise-limited node (a real directional effect the workload bench cannot resolve)
  gets an agent-authored isolation micro-bench. Gates: it builds and emits samples;
  its A/A floor beats the parent's; the target function owns at least 60% of its
  self-time (profiler-verified); it honors `ARO_BENCH_SCALE`. The micro-bench only
  replaces the measurement; correctness stays on the parent differential, and a
  micro-proven win folds only after a parent-workload non-regression check. A
  seeded-mutation check first proves the parent differential actually constrains the
  target function.
- **Workload factory** (`aro sweep --attempt --diverge --workloads N`): when the
  frontier is exhausted, an agent authors a new deterministic workload variant with
  its own differential oracle. Gates: same-seed determinism; a k=3 seeded-mutation
  test where every compiling mutation must alarm the new oracle; a coverage
  increment (at least one in-crate hot function the base workload never surfaced).
  Wins found under a synthetic workload carry regime `synthetic-workload` and are
  never auto-mergeable; whether the workload is representative is a human call.
- **Permanent tree** (`memory/permtree/`): a cross-run ledger of every
  (workload, function, baseline-state) node with its terminal verdict and evidence
  pointers. It computes the three exhaustion boundaries: the untouchable floor
  (crypto / runtime share), the measurement floor (every noise-limited node rescued
  or capped), and coverage closure (the workload factory goes dry). All three closed
  with headroom drained is the machine-checkable definition of "nothing provable
  left".

---

## Quickstart

Pure-stdlib Python (3.9+), driving Rust targets via cargo.

```sh
git clone https://github.com/RealiCZ/aro && cd aro
python3 selftest.py        # cargo-free check: 21 isolated case groups
python3 tests/e2e_fixture.py   # the real judge on a real crate (needs cargo)
```

**Spec-driven**, when you have already isolated the metric. A new target is a spec,
not code: one JSON file (`targets/<name>.json`) with 7 slots (`target_repo`,
`hot_path`, `metric`, `direction`, `benchmark_probe`, `correctness_oracle`,
`constraints`, plus a `run` block of loop knobs). The loop, judge and generator
never change.

```sh
# turn a free-form goal into a validated spec (detect -> fill slots + write probes -> dry-run)
python3 -m aro plan "make the scalar-mul faster" /path/to/repo

# or copy examples/target.example.json, fill the slots, then run it:
python3 -m aro run targets/<name>.json --rounds 3
#   --blind                    profiler-only hint (no technique named)
#   --aa-runs N --ab-pairs N   measurement power
#   --out DIR                  where events.jsonl lands
```

**Unattended, whole-frontier**: walk the profiled hot frontier, judge each function,
compound the wins, re-profile on top, until the frontier or the budget is spent:

```sh
python3 -m aro sweep targets/<name>.json                       # L1: the frontier map (report-only)
python3 -m aro sweep targets/<name>.json --attempt --diverge --critic
#   --critic        second judge (independent semantic reviewer)
#   --workloads N   grow up to N qualified workload variants when the frontier dries
#   --out-dir DIR   compounding wins land here; re-point at the same DIR to RESUME
```

**Report and hand-off**, derived from a run's `events.jsonl` without re-running:

```sh
python3 -m aro tree <out-dir>                    # the exhaustion-ledger report (decision-tree.html)
python3 -m aro manifest <out-dir>                # final accepted edit-set -> manifest.json
python3 -m aro serve <out-dir> --port 8010       # live-refreshing report over HTTP (127.0.0.1)
```

Worktrees are created from the frozen baseline under the target repo's
`.aro-worktrees/` and removed after each candidate; each gets its own
`CARGO_TARGET_DIR`. The cost is recompiling per candidate, which is the price of a
sound measurement.

---

## Generators

The spec's `generator` slot picks how candidates are produced; the judge is
identical either way:

- **`agentic`** (default): a live `claude` write-compile-fix loop with read and
  reflect phases. It can land multi-site refactors a one-shot patch cannot.
- **`ralph`**: a thin one-shot `claude -p` returning a block patch.
- **`PlannedGenerator`**: a seeded edit, used by `aro verify-patch` and the tests to
  re-score a recorded patch deterministically through the full judge.

---

## What it won't do (honest)

- **It cannot resolve a change below the noise floor.** A real sub-floor win
  measures `within-noise` or `noise-limited`. Raise `--aa-runs`/`--ab-pairs`, let
  the probe factory build a tighter bench, or accept that the gain is not provable
  here. Never lower the bar.
- **The generator is a model; only the judge is code.** Re-runs propose different
  patches. Reproducibility lives in the judge and its seeded statistics.
- **The metric must be isolable** behind a microbench. A kernel diluted in an
  end-to-end number cannot be optimized measurably.
- **Single-machine measurement.** Paired, order-alternated A/B cancels slow drift,
  not a busy box. Run on a quiet machine, and treat one round as weak evidence; the
  value is in multiple rounds compounding.

---

## Layout

| path | role |
|---|---|
| `aro/engine.py` | the loop (`RunConfig` + phase methods): freeze, resume, calibrate, generate, prescreen, judge, fold, reflect; compounds accepted patches into the baseline |
| `aro/eval.py` | the judge: A/A floor calibration, paired A/B, bootstrap CI, the three gates, prescreen with worktree hand-off |
| `aro/guard.py` / `aro/stats.py` | reward-hacking screen / median, quantile, seeded bootstrap CI |
| `aro/target.py` | `SpecTarget`, the generic driver: git-worktree isolation, build/test/bench/differential |
| `aro/profile.py` / `aro/symbols.py` | cross-platform CPU profiler / v0 demangling and owner classification |
| `aro/frontier.py` | workspace ownership, hot-fn bucketing, headroom arithmetic, the explorer's stop rule |
| `aro/attempt.py` | the unattended meta-loop (`aro sweep --attempt`), the probe rescue, the multi-workload campaign, finalize |
| `aro/sweep.py` | the L1 frontier map (report-only) and the profiling entry |
| `aro/probe_factory.py` | agent-authored isolation micro-benches behind a probe-judge |
| `aro/workload_factory.py` | agent-authored workload variants behind a workload-judge |
| `aro/permtree.py` | the permanent cross-run decision tree and the exhaustion proof |
| `aro/generator.py` | `agentic` / `ralph` / `PlannedGenerator` |
| `aro/critic.py` | the second judge: independent adversarial semantic review (`--critic`) |
| `aro/llm.py` / `aro/vcs.py` | the single claude invocation point (`ARO_CLAUDE_BIN`) / git plumbing with timeouts |
| `aro/runlog.py` / `aro/events.py` | the single events.jsonl reader / the structured event writer (source of truth) |
| `aro/patchfile.py` / `aro/store.py` | the SEARCH/REPLACE patch-format owner / records, pareto, floors (resumable) |
| `aro/spec.py` / `aro/types.py` | validated spec loader / core types and the one headline-delta rule |
| `aro/manifest.py` / `aro/tree.py` / `aro/chart.py` | run-to-PR hand-off / the exhaustion-ledger report (`aro/ledger_template.html`, no build step) / SVG figures |
| `aro/cli.py` / `aro/serve.py` / `aro/verify.py` | the argparse CLI surface / live HTTP report / re-score a recorded patch |
| `aro/plan.py` / `aro/context.py` / `aro/prompts.py` | goal-to-spec (`aro plan`) / code-context provider / prompt-template loader |
| `targets/*.json` / `probes/*.rs` / `fixtures/mini-target/` | specs / microbench probes / the cargo E2E fixture crate |
| `tests/e2e_fixture.py` / `selftest.py` | the real-judge E2E / 21 isolated cargo-free case groups |
| `memory/lessons.jsonl` / `memory/permtree/` | cross-run lessons / the permanent decision-tree ledger |
| `skill/` | the committable skill: prose docs (`references/`) and executed prompt templates (`prompts/`) |

---

ARO is inspired by Karpathy's [autoresearch](https://github.com/karpathy/autoresearch),
hardened for code where correctness is non-negotiable: it finds where the time really
goes, changes it, and believes only a win it can prove.
