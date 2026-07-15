# ARO: Auto-Research Optimizer

**Autonomous performance-bottleneck discovery for Rust, with an instruction-count judge.**
ARO profiles the real hot path, proposes behaviour-preserving candidates, and only believes
a win it can prove — correctness/integrity gates first, then significance (paired A/B and,
on supported hosts, callgrind Ir), with a multi-backend generator (`claude` / `codex` /
`grok`). Pure-stdlib Python, zero runtime dependencies; drives targets through cargo.

> **The loop is commodity; the judge is the moat.**
>
> Any coding agent can generate a candidate. The hard part is a deterministic evaluator
> that cannot be fooled on a sub-1% change buried in noise. On consensus / crypto / EVM
> code a faster-but-wrong change is a disaster, so behaviour must stay byte-identical
> (or explicitly downgraded). ARO puts the engineering weight on the judge.

**New target?** Start at [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — exploration tier vs
certification tier, probe contracts, and the full field table.
**Server / Ir / terminal ops:** [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

---

## Picking this up as an AI agent

- **Consuming a finished run** (e.g. turning wins into a PR): `python3 -m aro manifest <out-dir>`.
  `manifest.json` is the accepted edit set with provenance and a `mergeable` flag.
  `accepted` ≠ should-merge: mergeable further requires regime + critic rules, and when the
  target declares `terminal_bench_targets`, a tool-written `terminal_stamp` with
  `TERMINAL_CONFIRMED` (see OPERATIONS §12–13). Data contract:
  [`skill/references/run-data.md`](skill/references/run-data.md).
- **Operating ARO**: [`skill/SKILL.md`](skill/SKILL.md) indexes subcommands and protocol docs
  under `skill/references/`. Day-to-day short index: [`OPERATING.md`](OPERATING.md).

A run's source of truth is `events.jsonl` (append-only). Everything else
(`manifest.json`, HTML report, charts) is derived and regenerable via `aro tree` /
`aro manifest`.

---

## What the judge catches

Real examples from this repo's runs:

- Multi-site behaviour-preserving optimization verified as a **+14% win**: clear of the
  noise floor, differential byte-identical, accepted.
- A **-53% regression** that only the judge caught: tests and DIFF passed; paired A/B
  with CI showed it was slower.
- Shared-build-dir bug once masked deltas near zero; per-worktree `CARGO_TARGET_DIR`
  fixed it.

Lessons accumulate in [`memory/lessons.jsonl`](memory/lessons.jsonl).

---

## How it works

```
observe -> read -> generate -> judge -> record -> reflect -> (goal met / dry? -> stop)
                                 ^                                   |
                                 +-------- compound + next round ----+
```

- **observe**: CPU profiler (macOS `sample`; Linux `perf`) ranks hot in-binary functions.
- **read**: plan one behaviour-preserving change on the measured hot path.
- **generate**: write-compile-fix in a throwaway git worktree (agentic default; multi-backend).
- **judge**: Gate 0 path screen → Gate 1 build/test/DIFF → significance (paired A/B +
  bootstrap CI; Ir Gate 1.5 when valgrind is available) → optional critic → terminal
  criterion-Ir before PR when configured.
- **record / reflect**: accepts compound into the working baseline; agenda + lessons persist.

---

## The judge (summary)

Code: `aro/eval.py`, `aro/stats.py`, `aro/guard.py`; Ir/terminal: OPERATIONS §13.

| Gate | What |
|---|---|
| **0** Reward-hacking guard | Path-only: no `Cargo.toml`/`lock`, no `benches/`/`tests/`, stay in editable regions |
| **1** Correctness | Build + test (pass count) + random-input **DIFF** vs frozen baseline |
| **1.5** Probe Ir | Callgrind instruction counts when host tooling is present |
| **2** Significance | Paired order-alternated A/B, A/A floor, bootstrap CI |
| **Terminal** | Pre-PR criterion row Ir via `measure_bin` when `terminal_bench_targets` is set |
| **Critic** | Optional second semantic judge (`--critic`) — AND, not OR |

Each worktree gets its own `CARGO_TARGET_DIR`. Prescreen survivors hand built worktrees to
the judge so nothing is compiled twice.

---

## Self-extending search

When the frontier stalls, ARO can grow measurement tools and workloads behind deterministic
qualification gates (frozen sha256 before generation):

- **Probe factory** (`--diverge` / `--probe-factory`): isolation micro-benches for noise-limited nodes.
- **Workload factory** (`--workloads N`): synthetic workload variants; wins tagged
  `synthetic-workload`, never auto-mergeable.
- **Permanent tree** (`memory/permtree/`): cross-run ledger and exhaustion boundaries.

---

## 30-second quickstart

```sh
git clone <this-repo> && cd aro-py
python3 selftest.py                 # cargo-free check
python3 tests/e2e_fixture.py        # real judge on fixtures/mini-target (needs cargo)

# Point ARO at YOUR Rust project (exploration tier):
python3 -m aro init --repo /path/to/rust-repo [--package <crate>]
# → fill probe TODOs, then:
python3 -m aro selfcheck targets/<name>.json
python3 -m aro sweep targets/<name>.json --attempt --out-dir ./.aro-runs/<name>
```

Full walkthrough, BENCH/DIFF contracts, and field table:
**[`docs/ONBOARDING.md`](docs/ONBOARDING.md)**.

Spec-driven loop (already have probes):

```sh
python3 -m aro plan "make the scalar-mul faster" /path/to/repo   # agent-assisted spec
python3 -m aro run targets/<name>.json --rounds 3
python3 -m aro sweep targets/<name>.json                         # L1 map only
python3 -m aro sweep targets/<name>.json --attempt --diverge --critic
```

---

## CLI surface

Production core:

| Command | Role |
|---|---|
| `aro init` | Scaffold minimal spec + two probe templates |
| `aro sweep` | L1 frontier map; `--attempt` unattended meta-loop |
| `aro terminal` | Pre-PR criterion Ir gate (`--rejudge` offline; `--calibrate` floors) |
| `aro selfcheck` | Host measurement health + tool pins; gate precondition |
| `aro manifest` | Accepted edit-set → `manifest.json` (+ terminal mergeability) |
| `aro recheck` | Namespace: `staleness` / `debts` / `candidates` (baseline churn, open-debt Ir, manifest re-gate) |
| `aro ablate` | Per-entry terminal attribution; shippable sub-bundle proposal |
| `aro serve` | Live HTTP report (default `127.0.0.1:8010`) |

Supporting: `aro tree`. Soft-deprecated (still work; one stderr warning): `run`, `plan`, `union`, `next`, `coverage`, `clean`, `verify-patch`, `hotpath`. Aliases (one stderr note): `reverify` → `recheck candidates`, `recheck-debts` → `recheck debts`, bare `recheck` → `recheck staleness`, `terminal-calibrate` → `terminal --calibrate`.

Flags and env details: `python3 -m aro <cmd> -h`, [`skill/SKILL.md`](skill/SKILL.md),
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

---

## Generators

Spec `generator` / CLI `--generator`; judge is identical either way:

- **`agentic`** (default): write-compile-fix on the selected LLM backend + read/reflect.
- **`ralph`**: thin one-shot patch.
- **`PlannedGenerator`**: seeded edit for `verify-patch` / tests.

Backend: top-level `llm_backend` or `ARO_LLM_BACKEND` (`claude` / `codex` / `grok`).
Optional `critic_backend` for cross-model review.

---

## What it won't do (honest)

- **Cannot certify below composition / floor limits.** Probe Ir ε and terminal row floors
  are hard; shared-bench composition bounds sub-1% terminal claims (ONBOARDING § Honest limits;
  OPERATIONS control-lane / A-A protocol).
- **Generator is a model; only the judge is code.** Reproducibility lives in the judge.
- **Metric must be isolable** behind a microbench.
- **Single-machine measurement.** Quiet host; multiple rounds compound.

---

## Layout

| path | role |
|---|---|
| `aro/engine.py` | Loop: freeze, resume, calibrate, generate, prescreen, judge, fold, reflect |
| `aro/eval.py` | Judge: A/A, paired A/B, gates, prescreen hand-off |
| `aro/guard.py` / `aro/stats.py` | Reward-hacking screen / bootstrap CI |
| `aro/target.py` | `SpecTarget`: worktrees, build/test/bench/DIFF |
| `aro/terminal.py` / `aro/selfcheck.py` | Criterion Ir gate + floors; host health marker |
| `aro/init.py` | Target scaffolder |
| `aro/attempt.py` / `aro/sweep.py` | Unattended meta-loop / L1 map |
| `aro/probe_factory.py` / `aro/workload_factory.py` | Self-grown benches / workloads |
| `aro/manifest.py` / `aro/ablate.py` / `aro/reverify.py` | Hand-off, attribution, re-gate |
| `aro/cli.py` | Argparse subcommand registry |
| `targets/*.json` / `probes/*.rs` | Specs / microbench + DIFF probes |
| `docs/ONBOARDING.md` / `docs/OPERATIONS.md` | Onboard any Rust repo / server Ir runbook |
| `docs/archive/` | Historical design docs (not current behaviour) |
| `skill/` | Operator skill: `references/` prose + `prompts/` templates |
| `tests/` / `selftest.py` | Cargo-free + domain selftests |

---

ARO is inspired by Karpathy's [autoresearch](https://github.com/karpathy/autoresearch),
hardened for code where correctness is non-negotiable: find where the time goes, change it,
and believe only a win you can prove.
