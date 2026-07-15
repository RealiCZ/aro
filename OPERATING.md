# ARO Operating Manual

Short day-to-day index. Prefer the longer docs when they disagree with this file:

| Task | Doc |
|---|---|
| Onboard a new Rust repo (probes, tiers, field table) | [`docs/ONBOARDING.md`](docs/ONBOARDING.md) |
| Server host, Ir/terminal, selfcheck, control lanes | [`docs/OPERATIONS.md`](docs/OPERATIONS.md) |
| What ARO is + CLI surface | [`README.md`](README.md) |
| Protocol depth (judge, harness, run→PR) | [`skill/SKILL.md`](skill/SKILL.md) |

## Mental model

ARO is a **goal-driven loop**: observe hot path → plan → agentic generate in a worktree →
**judge** (correctness + significance + optional Ir/terminal) → record → reflect → stop on
goal or dry streak.

- Thin / prompt-driven: orchestration, generation, per-target knowledge (the spec).
- Deterministic moat (`aro/`): guard, eval, stats, terminal, selfcheck — never graded by the model.

**Onboarding a new repository** = `aro init` (or a hand-written `targets/*.json`) + two probes,
not new Python. Exploration tier first; certification (terminal gate) is additive — see ONBOARDING.

## Prerequisites (minimum)

- Python 3.9+, stdlib only; `cargo` + `git` on PATH.
- Target builds and tests green standalone.
- Profiler: macOS `/usr/bin/sample` or Linux `perf`.
- Authenticated LLM CLI (`llm_backend`: claude / codex / grok). Provisioning: OPERATIONS §1.
- Ir / terminal paths: Linux host + valgrind/codspeed pins + `aro selfcheck` marker (OPERATIONS §13).

Each worktree gets its **own** `CARGO_TARGET_DIR` (`.aro-<spec.name>-td/<worktree>`).
Shared target dirs collapse deltas. Worktrees live under `.aro-worktrees/` and are removed when done.

## Common commands

```sh
# Scaffold exploration tier
python3 -m aro init --repo /path/to/rust [--package <crate>]

# Host health (required before icount/terminal/calibrate)
python3 -m aro selfcheck targets/<name>.json

# Frontier map (no LLM)
python3 -m aro sweep targets/<name>.json --min-pct 1.5

# Unattended campaign
python3 -m aro sweep targets/<name>.json --attempt --diverge --critic \
    --out-dir ./.aro-runs/<campaign>

# Harvest / ship path
python3 -m aro manifest ./.aro-runs/<campaign> --spec targets/<name>.json
python3 -m aro terminal targets/<name>.json --baseline <wt> --candidate <wt> \
    --out terminal.json --update-manifest ./.aro-runs/<campaign>
python3 -m aro tree ./.aro-runs/<campaign>
python3 -m aro serve ./.aro-runs/<campaign>   # 127.0.0.1:8010
```

Single-path loop (fixed hot_path): `python3 -m aro run targets/<name>.json [--rounds N]`.
Full CLI list: README § CLI surface; `python3 -m aro -h`.

## Spec (7 slots + knobs)

Authored slots: `target_repo`, `hot_path`, `metric`, `direction`, `benchmark_probe`,
`correctness_oracle`, `constraints`, plus optional `run` and certification/policy fields.
Loader: `aro/spec.py`. Field table: ONBOARDING. Template: `examples/target.example.json`.
Worked certification example: `targets/mega-evm-v2.json`.

**Differential is enforced by default**: missing DIFF → `verify-failed` unless
`constraints.weak_oracle=true` (tests-only; verdict tagged `WEAK ORACLE`).

## Reading output (`--out` / `--out-dir`)

| file | what |
|---|---|
| `events.jsonl` | Source of truth (append-only event stream) |
| `REPORT.md` | Sweep attempt narrative (written by code during `--attempt`) |
| `decision-tree.html` / `tree.json` | Exhaustion ledger (`aro tree` / `aro serve`) |
| `manifest.json` | Accepted edit-set + mergeable (`aro manifest`) |
| `terminal.json` | Criterion Ir adjudication (when run) |
| `aN/patches/`, `records.jsonl`, `floors` | Per-attempt patches / ledger / noise floors |

**Verdicts** (inner loop): `accepted` / `within-noise` / `noise-limited` / `regressed` /
`verify-failed` / `build-failed` / `rejected`. Terminal outcomes: `TERMINAL_CONFIRMED`,
`TERMINAL_CONFIRMED_WITH_TRADE`, `TERMINAL_CONTROL_ANOMALY`, … (OPERATIONS §13).

## Resume

Same `--out-dir` resumes from the advanced baseline (compounded accepts).
Fresh directory = clean start. `aro run` has `--ignore-resume-failure`; sweep does not.

## Known limits

- Measurement is host-dependent; run selfcheck; quiet box for wall-clock paths.
- Shared-bench composition bounds tight terminal claims — A/A before raising
  `control_composition_bound_pct` (OPERATIONS §13.4; ONBOARDING § Honest limits).
- Weak oracle forfeits byte-identical proof.
- Large refactors need read phase + compounding; one shot can still be slow.
