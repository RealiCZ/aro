# ARO Operating Manual

How to run it, how to onboard a new target, and how to read the output. Architecture and the loop protocol are in `skill/SKILL.md`; unattended operation (the agent locates the hot path and writes the probe itself) is in `skill/references/autonomous-optimization.md`.

## 0. Mental model

ARO is a **goal-driven loop**: observe the hot path → read the code and produce a plan → implement via an agentic write-compile-fix loop → **judge** (correctness + significance) → write memory → **reflect into the next research direction (the agenda)** → repeat until the goal is met or the gains dry up.

- **The thin, prompt-driven part**: orchestration, generation, code reading, per-target knowledge (the spec).
- **The small deterministic core (executed code, `aro/`)**: judging (`eval`), statistics (`stats`), anti-cheat (`guard`), the measurement protocol. **This part must be executed, never inferred by a prompt**: the code writer cannot grade itself, the statistics must be reproducible, and the verdict must be impossible to talk around. This is the moat.

**Onboarding a new repository = writing one spec (`targets/*.json`), not writing code.** The loop is the same for every target.

## 1. Prerequisites

- Python 3.9+, standard library only (zero external dependencies).
- The target repository builds with `cargo build --release`; `cargo` and `git` are on PATH.
- macOS: the profiler is the built-in `/usr/bin/sample` (no sudo needed).
- The `claude` CLI: used by the read phase (read-only) and by the agentic generator (writes, inside a throwaway worktree with `--dangerously-skip-permissions`, deleted after the run).

Each worktree gets its **own** `CARGO_TARGET_DIR` (`.aro-<spec.name>-td/<worktree>`). A shared target dir would let cargo reuse compiled artifacts across worktrees, so baseline and candidate would end up comparing the same binary (the delta and the differential would both be meaningless). The cost is one extra compile per candidate; that is a necessary price for correctness. Worktrees live in `.aro-worktrees/` and are deleted when done.

## 2. Main command: `python3 -m aro run`

```sh
cd aro
python3 -m aro run targets/<name>.json \
    [--rounds N] [--blind] [--no-read] [--aa-runs N] [--ab-pairs N] [--out DIR]
```

| flag | default | meaning |
|---|---|---|
| `<spec.json>` | (required) | the target spec |
| `--rounds N` | spec.stop.max_rounds | hard cap on rounds (goal/dry stops can end the run earlier) |
| `--blind` | (off) | use a profiler-only hint (does not name the trick), for an honest blind-discovery test |
| `--generator ralph\|agentic` | spec.generator (default agentic) | thin one-shot `claude -p` vs heavy write-compile-fix (+read+reflect) |
| `--no-read` | (off) | skip the read phase |
| `--aa-runs N` | 2 | A/A calibration pair count |
| `--ab-pairs N` | 4 | paired A/B count per candidate |
| `--out DIR` | `./.aro-runs/<name>` | output directory |
| `--ignore-resume-failure` | (off) | on resume, if reapplying the accepted patches fails, continue from the original baseline instead of aborting |

Generation defaults to the **agentic write-compile-fix** loop (real `claude`): each round it edits, builds, tests, fixes, and iterates inside a throwaway worktree, and **stops on its own goal** (done once build+test pass; there is only a very high hang backstop, not a work cap). ARO takes the final diff and hands it to the judge.

## 3. Onboarding a new target (writing a spec)

An authored spec has **7 slots** (schema in `skill/references/spec-slots.md`):
- **`target_repo`** `{path, baseline_ref}`;
- **`hot_path`** `{file, fn}`: what to optimize (fed to the generator; also the default value of `editable`);
- **`metric`** + **`direction`** (minimize/maximize): what counts as a win;
- **`benchmark_probe`** `{pkg, probe, example, sample_prefix, profile}`: how to measure (`probes/*.rs`);
- **`correctness_oracle`** `{build, test, differential}`: how to prove behavior is unchanged;
- **`constraints`** `{editable, no_new_deps, byte_identical, notes, weak_oracle}`: the editable surface plus the hard rules;
- the `run` block: `generator` / `goal_target` / `stop{max_rounds,dry_rounds}` / `aa_runs` / `ab_pairs` / `timeout`.

`objectives` / `goal` are **derived** from `metric+direction+goal_target`; do not write them twice. `goal_target=null` means open-ended (best effort, bounded by `stop`). Two ways to produce a spec:
- `python3 -m aro plan "<goal>" <repo>`: detect the commands → an agent fills the judge slots and writes the probe → **dry-run build+probe+test+differential** → print the slot dump → write the spec (see `plan-workflow.md`);
- copy `examples/target.example.json` and fill it in by hand.

**The differential is enforced by default**: without a `benchmark_probe.differential` probe the verdict is `verify-failed` outright (a test suite is not a byte-identical proof). Only an explicit `constraints.weak_oracle=true` downgrades this to a tests-only check, and the verdict is then tagged `WEAK ORACLE`.

## 4. Tools

```sh
python3 find_hotpath.py targets/<spec>.json              # find the real hot path + isolated kernel latency (spec required)
python3 verify_patch.py <patch.txt> --spec <spec.json> [--ab-pairs N] [--aa-runs N] [--out DIR] [--reuse-out]   # re-score a recorded patch through the full judge
python3 selftest.py                                      # cargo-free mock self-test, 21 isolated case groups
```

`find_hotpath.py` and `verify_patch.py` are thin shims over the `aro hotpath` and `aro verify-patch` subcommands; `--spec` is required for `verify-patch`.

## 5. Reading the output (the `--out` directory)

| file | what it is |
|---|---|
| `events.jsonl` | **the source of truth**: the step-by-step event stream (including `regression_baseline` / `read_phase` / `gate` / `candidate_verdict` / `baseline_advanced` / `direction_proposed` / `goal_met` / `stopped`), flushed in real time, so you can `tail -f` it |
| `RUN-REPORT.md` | the human-readable narrative (English), **rendered by the skill from `events.jsonl`** (numbers copied verbatim; there is no `report.py`; see `skill/references/report-protocol.md`). Note this is a different file from the `REPORT.md` that `aro sweep --attempt` writes directly from code: two paths, two files |
| `decision-tree.html` / `tree.json` | the exhaustion ledger. `aro sweep --attempt` writes it automatically; render or refresh it by hand with `python3 -m aro tree <out-dir>`; serve it live with `python3 -m aro serve <out-dir>` (default `127.0.0.1:8010`; pass `--host 0.0.0.0` explicitly to expose it on the network, unauthenticated) |
| `records.jsonl` / `floors.json` / `agenda.jsonl` / `patches/<id>.txt` | the memory ledger / the noise floors / the research agenda / the raw patches |

**Verdicts**: `accepted` (passed both gates, enters the Pareto set) / `within-noise` / `regressed` / `verify-failed` (tests failed / passing tests dropped below the baseline count N_pre / differential mismatch) / `build-failed` / `rejected` (blocked by anti-cheat, never ran).

## 6. Memory and resume

Running again with the same `--out` **rebuilds the accepted patches** (from `pareto` + `patches/`) and applies them to the baseline: a resumed run continues from the **already-optimized baseline**, not from scratch (`baseline_resumed`); dead ends also feed the next round's prompt. If reapplying fails, the run aborts rather than silently optimizing the original code (`--ignore-resume-failure` overrides this and continues from the original baseline). `events.jsonl` appends per `run_id` (never truncated, no history is lost). For a clean start, use a new `--out`.

## 7. Known limits

- **Measurement depends on the machine**: the A/A floor is different every round; to draw conclusions, use a quiet machine and give `--ab-pairs` enough budget.
- **The differential**: ARO runs the same deterministic random-input probe on both baseline and candidate and requires identical output, a true byte-for-byte behavior check. **Enforced by default**: no declared `differential` probe means `verify-failed`, unless `constraints.weak_oracle=true` explicitly downgrades it (the verdict is tagged `WEAK ORACLE`).
- **Large refactors land through the read phase + no work cap + compounding**; a single `claude` call can still be slow.
- A win on an isolated micro-benchmark does not necessarily equal a win at production scale (especially for DRAM-bound kernels).
