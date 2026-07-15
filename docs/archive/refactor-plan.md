# ARO Handover Assessment and Refactor Plan (v1, pending review)

> **Historical design document** — may not reflect the current system. See [OPERATIONS.md](../OPERATIONS.md) and [ONBOARDING.md](../ONBOARDING.md) for what ships today.


*Status: executed. All phases (P0..P5) landed on branch refactor-2026-07; kept as the decision record.*

> Conclusion up front: **the judge (the moat) lives up to its name and stays essentially untouched; what needs to change is everything around it.**
> The project's core assets are the deterministic judging in `eval.py`/`stats.py`/`guard.py` plus the architectural decision to make `events.jsonl` the single
> source of truth; both are high quality. The main debt: a god module (`sweep.py`, 1049 lines),
> a giant function (`run_backtest`, 290 lines / 17 parameters), six kinds of cross-module copy-paste, zero tests on the real I/O boundaries
> (cargo/git/claude/profiler), zero CI and zero packaging, and several robustness gaps.
> The plan has 6 phases; each phase merges and rolls back independently, and it leaves a seam
> for infinite-flow phase 2 (producer-consumer).

---

## 0. Project snapshot

- **What it is**: ARO is an autonomous performance optimization loop: profile to find real hot spots → LLM generates one
  behavior-preserving change → deterministic three-gate judge (anti-cheat guard / correctness including byte-identical differential / A/A floor +
  paired A/B + bootstrap CI significance) → accepted patches fold into the baseline and compound → reflect feeds the next round.
  Pure stdlib Python (~5.5k lines) driving a Rust target; a Svelte frontend renders the decision-tree report.
- **Current branch** `infinite-flow-phase1` has landed infinite-flow phase 1 (parallel fan-out generation, prescreen,
  exhaustive frontier, automatic decision tree); the design doc plans phase 2 (cross-function producer-consumer, adversarial re-review).
- **Tests**: `selftest.py` is 799 lines, 22 test groups (#5-#27). Pure-logic coverage is decent;
  **cargo/git/claude/profiler real I/O has zero coverage**; no CI, no packaging, no lint or type checking.

---

## 1. Assessment

### 1.1 What is done well (must survive the refactor)

1. **The judge is a real moat**: A/A calibration floor, order-alternating paired A/B, seeded bootstrap CI,
   direction-aware verdicts, auto-tighten's defense against "swap the probe and go fishing" (sign consistency + the floor must drop),
   per-worktree `CARGO_TARGET_DIR` + forced-recompile self-check. The engineering density is far beyond a typical "AI optimizer".
2. **`events.jsonl` as the single source of truth**: every report/manifest/chart can be regenerated offline, and the discipline of
   "numbers verbatim, verdicts never re-judged" holds throughout.
3. **A culture of honesty**: `accepted ≠ should-merge`, no silent caps (prescreen drops are also recorded as outcomes),
   comprehension-debt listed explicitly, and a "What it won't do" section in the README.
4. The composition decisions are right: parallel generation / serial judging, end-of-round folding (siblings compete fairly),
   and resume failures fail fast instead of silently degrading (`engine.py:104-113`).

### 1.2 Problem list (by severity)

**A. Correctness risks (different artifacts can reach different conclusions)**

| # | Problem | Evidence |
|---|---|---|
| A1 | The "take the latest run slice" logic over `events.jsonl` has **3 different implementations**, and chart/sweep each have their own non-slicing reader: from the same log, different artifacts can read different runs | `manifest.py:30` `tree.py:24` `trajectory.py:55` `chart.py:280` `sweep.py:954` |
| A2 | The direction-aware "pick the headline Δ" selection logic is **written 5 times with inconsistent rules**: the same run's headline number changes with the rendering entry point | `store.py:232` `manifest.py:48` `trajectory.py:89` `chart.py:294` `__main__.py:105` |
| A3 | The SEARCH/REPLACE patch format has **2 parsers**, and `manifest`/`tree` import the private `store._parse_patch_file` function directly: one format change is guaranteed to break one of them | `store.py:255` vs `verify_patch.py:24`; `manifest.py:67` `tree.py:39` |

**B. Structural debt**

| # | Problem | Evidence |
|---|---|---|
| B1 | `sweep.py` is a 1049-line god module: v0 symbol demangling, owner classification, lesson indexing, frontier bucketing, profile orchestration, the L3 meta loop, 3 Markdown renderers, SVG→PNG, hand-rolled argv parsing | all of `aro/sweep.py` |
| B2 | `run_backtest` is a single 290-line function with 17 parameters; prescreen/folding/reflect/stopping are all inlined | `engine.py:36-327` |
| B3 | `SpecTarget` is a god object whose "private" boundary is fiction: 5 modules call `_td_for/_env/_pkg_dir/_write_probe/_run_diff_probe` directly | `sweep.py:350,359,368,450` `plan.py:133` `generator.py:303` `find_hotpath.py:40` |
| B4 | The `claude` subprocess call is **copied 5 times** (timeouts and cwd all differ); the git worktree lifecycle is **copied 3 times** | `generator.py:180,308,383,420` `critic.py:140`; `target.py:72` `plan.py:143` `generator.py:163,286` |
| B5 | CLI: hand-rolled `opt()` parsing **copied 8 times**, if-chain dispatch, two different styles for boolean flags vs value flags, unknown flags silently ignored | `__main__.py:24` `chart.py:516` `serve.py:63` `plan.py:235` `sweep.py:973` `verify_patch.py:48` and more |
| B6 | Two parallel chart stacks: `trajectory.py + chart.svg/ascii` (used only by `aro chart`) and `chart.perf_token_svg/explore_svg` (used by the real reports), each deriving the compounding curve from events on its own | `trajectory.py` `chart.py` |

**C. Robustness gaps**

| # | Problem | Evidence |
|---|---|---|
| C1 | All git subprocesses run with **no timeout** (cargo and claude both have one); a credential prompt or lock contention hangs the whole harness | `target.py:75,92,154,226` `generator.py:165,288,293` `plan.py:147,155` `verify_patch.py:81,85` |
| C2 | Generator bare `except → return None` **silently swallows candidates** without emitting an event: a systematically broken generator is indistinguishable from "the model made no proposal" | `generator.py:146,183,296,313,386,423` |
| C3 | Spec loading is unvalidated: when a required key like `bench["pkg"]` is missing, it surfaces as a KeyError deep inside `target.bench` | `spec.py:42`, multiple places in `target.py` |
| C4 | Events are schema-less bare dicts end to end; consumers rely on string comparison; a mistyped emit key fails silently | `events.py:58` plus every consumer |
| C5 | The profiler hardcodes the shared paths `/tmp/aro_sample.txt` / `/tmp/aro_perf.data`: **two concurrent runs overwrite each other** (in direct conflict with infinite-flow's parallelism goals) | `profile.py:119,144` |
| C6 | With no probe, `differential` runs `git status`, never looks at the result, and does `return True` (a dead check; strict mode is blocked at the eval layer and only the weak_oracle path can reach it, but the code itself is a trap) | `target.py:146-158` |
| C7 | `prompts.load` has no missing-file protection for the core templates (ralph/agentic/critic-*); `serve` binds `0.0.0.0` by default with no auth | `prompts.py:23` `serve.py:66-78` |

**D. Missing engineering basics**

- D1 No CI: `selftest.py` never runs automatically; there is no merge gate of any kind.
- D2 No `pyproject.toml`/packaging: not installable; `REPO_ROOT = Path(__file__).parent.parent` is computed
  separately in 4 modules (`spec.py:23` `plan.py:30` `prompts.py:20` `lessons.py:17`), so the package cannot be relocated.
- D3 No ruff/mypy: the whole codebase carries full type annotations that nothing checks (wasted effort).
- D4 selftest is a single 799-line `run()` with bare asserts; the first failure masks everything after it; real I/O boundaries have zero coverage.

**E. Repo hygiene / documentation drift**

- E1 `.gitignore` is only 3 lines; `.aro-report-8010/` (741KB of generated output) is not ignored; `remote-readme.md`
  is a host inventory unrelated to this repo (probably dropped in by mistake).
- E2 The machine-appended `memory/lessons.jsonl` is git-tracked and carries a long-lived uncommitted diff; a 512KB build artifact
  `aro/decision_tree_template.html` and a 617KB PNG are checked in.
- E3 Documentation drift: the `find_hotpath.py` usage in `OPERATING.md:64` is missing a required argument;
  `docs/archive/explore-mode-design.md:136` marks the already-shipped critic as "to be built";
  the human-facing report goes by three names (`RUN-REPORT.md`/`REPORT.md`/`DAILY-REPORT.md`).
- E4 Generated reports mix Chinese and English (the `sweep.py` labels meaning "evolved" / "can evolve" / "hands off", and the Chinese critic_context),
  against the recent English-only skill policy.
- Dead code: `Candidate.parent`, `Report.log/rounds/floors` (written but never read), the critic multi-reviewer
  `n>1` path has no users, and `eval.py:272-273` is an unreachable branch.

---

## 2. Refactor principles (Not building / frozen zone)

1. **Judge semantics frozen**: the verdict logic, thresholds, and statistical definitions in `eval.py`/`stats.py`/`guard.py`/`critic.py`
   do not change; moving code and purely mechanical tidying are allowed, and any semantic change is out of scope for this plan.
2. **The event contract stays unbroken**: existing `events.jsonl` fields are add-only, never changed or removed (downstream skills/consumers depend on them).
3. **The five invariants stay enforced** (infinite-flow design §6): serial bench, writers never judge themselves, correctness before
   significance, numbers verbatim, generality goes through cargo metadata.
4. **No rewrite of the viz frontend** (1.2k lines of Svelte, in good shape); infinite-flow phase 2
   (producer-consumer, adversarial re-review, multi-workload) is not part of this plan, but P3 leaves the seam ready for it.
5. **No runtime third-party dependencies** ("pure stdlib" is a product promise); dev tools (ruff) go into CI only.
6. **The directory stays flat**: sub-packaging into `core/infra/judge/loop/report` was considered and **rejected**: 24 flat modules
   are perfectly manageable, and sub-packaging only breaks git blame and invalidates every documented path, with no real payoff.

---

## 3. Phased plan (each phase independently mergeable, independently revertible)

### P0: hygiene and docs (~0.5 days)

- Add to `.gitignore`: `.aro-report-*/`, `.aro-worktrees/`, `*.egg-info/`.
- Fix the 3 documentation drifts (E3): the find_hotpath usage in OPERATING.md, the critic status table in
  explore-mode-design.md (or mark it "superseded by infinite-flow-design.md"), and a unified glossary of report names.
- Dispose of `remote-readme.md` (open question Q4; default is to move it out of the repo).
- Unify generated-report language to English (E4, open question Q3): change only the user-visible strings in
  `sweep.py`/`critic_context`; the Chinese design docs stay as they are.
- **Verification**: `python3 selftest.py` all green; `git status` clean.

### P1: safety net (~1.5 days) ★ prerequisite for everything after

- `pyproject.toml`: `requires-python = ">=3.9"`, package `aro`, declare `skill/prompts/*.md` and
  `probes/` as package data (declare now; the path-resolution migration happens in P5).
- `ruff` with a minimal rule set (E/F/W + isort), **no whole-repo reformat**, only block new violations.
- GitHub Actions: job 1 runs `python3 selftest.py` (3.9 and 3.12 matrix) + ruff;
  job 2 installs the Rust toolchain and runs the new **cargo fixture E2E**. CI checks only; no pushing, no publishing.
- **cargo fixture E2E (the core of this phase)**: put a small crate of a few dozen lines in `fixtures/mini-target/`
  (with a bench example, a differential probe, and 2 tests) plus a matching spec. Use `PlannedGenerator` to seed a
  known patch and walk the full real chain: `make_worktree → build → test → differential →
  calibrate_floors → evaluate → manifest`. Auto-skip when cargo is not available locally.
  This is the only net that can catch the real `target.py`/judge paths: **it must land before P2/P3**.
- **Verification**: both CI jobs green; the fixture E2E passes on this machine (which has cargo).

### P2: deduplicate and merge (~2.5 days, 6 independent commits)

1. **`aro/runlog.py`**: `load_events(dir)` + `latest_slice(events)` (the single slicing rule,
   keyed on `run_started`+`run_id`) + a constants table for event and field names. Repoint the five call sites in `manifest`/`tree`/
   `trajectory`/`chart`/`sweep` (kills A1 and half of C4).
2. **`aro/patchfile.py`**: the single owner of the SEARCH/REPLACE format (dump/parse/safe-id).
   Repoint `store`/`manifest`/`tree`/`verify_patch` (kills A3).
3. **Unify headline-Δ selection**: add a single function `best_improvement(deltas, obj_min)` to `types.py`
   and repoint all 5 call sites (kills A2).
4. **`aro/llm.py`**: `run_claude(prompt, *, cwd, timeout, session_log=None) →
   (text, tokens, cost_usd)`, unifying the 5 call sites; failures **must emit an event** (`generator_error`,
   a new event, does not break the old contract) instead of returning a silent None (kills the claude half of B4 plus C2).
5. **`aro/vcs.py`**: a thin wrapper with timeouts over git worktree add/remove/status/rev-parse;
   repoint `target`/`plan`/`generator`/`verify_patch` (kills the git half of B4 plus C1).
6. **Validate specs on load**: `spec.from_dict` checks the required keys `bench.pkg/example`, `differential.*`,
   `profile.*` and, when one is missing, reports "which slot is missing which key" (kills C3).
- **Verification**: run selftest + fixture E2E on every commit; diff one real spec's
  `aro manifest`/`aro tree` output against pre-refactor, byte-identical (same events.jsonl input).

### P3: structural split (~4 days)

1. **Split `sweep.py` (1049 lines → 4 modules)**:
   - `aro/symbols.py`: v0 demangling, `_fn_name`, `classify_owner`, rustfilt integration (~250 lines, pure functions).
   - `aro/frontier.py`: `bucket_functions`, lesson indexing, `_refill_queue`, headroom computation (pure functions).
   - `aro/attempt.py`: the L3 meta loop `attempt()` + `_finalize_run` (orchestration layer).
   - `sweep.py` keeps only the L1 frontier map (profile_ranked + render_map).
   - Markdown rendering (`render_map/render_attempt_map/render_explore_report`) merges into
     `aro/report_md.py`; `_svg_to_png` moves into `chart.py`.
2. **`__main__.py` → an argparse subcommand registry**: each subcommand module exposes
   `register(subparsers)`; delete the 8 copies of `opt()`; unknown flags error out instead of being silently ignored.
   `verify_patch.py`/`find_hotpath.py` are folded in as the `aro verify-patch`/`aro hotpath`
   subcommands (a one-line shim stays at the repo root so the README usage keeps working).
3. **Slim `run_backtest`**: add a `RunConfig` dataclass that gathers the 11 loop knobs; split the body into
   `_freeze_baseline` / `_resume` / `_prescreen_round` / `_judge_round` / `_fold_round`
   / `_reflect_round`; behavior and the event stream stay **byte-identical** (verified by diffing the
   fixture E2E's events.jsonl).
4. **Legitimize the `SpecTarget` boundary**: the externally called `_td_for/_env/_pkg_dir/_write_probe/
   _run_diff_probe` lose their underscores, become public, and get docstrings; the `cargo metadata` query moves into a standalone
   `aro/cargo.py` (`sweep._workspace_members` currently stuffs its cache into `target.__dict__`; fold that in too).
5. **Leave the seam for infinite-flow phase 2**: after P3, the generation side (`generator.propose`) and the judging side
   (`eval.prescreen + evaluate`) communicate only through explicit parameters, with no shared mutable state:
   the phase 2 producer-consumer queue can be inserted directly between them, without splitting sweep first.
- **Verification**: selftest + fixture E2E; a dry run of one `--attempt` round (PlannedGenerator) on the same spec
  produces an identical events.jsonl event sequence.

### P4: test upgrade + robustness (~2.5 days)

- Split `selftest.py` into `tests/test_*.py` (**stdlib `unittest`**, no pytest, keeping the zero-dependency
  promise; `python3 selftest.py` stays as a one-line wrapper around `unittest discover`, so the README usage does not break).
- Robustness fixes:
  - profiler temp files → a per-run `tempfile.mkdtemp` (kills C5, unlocks parallel runs);
  - `prompts.load` reports the list of available templates when one is missing (kills half of C7);
  - `serve` binds `127.0.0.1` by default; `--host 0.0.0.0` must be given explicitly (kills the other half of C7);
  - `target.differential` makes the no-probe path explicit (under weak_oracle, return True directly plus a comment;
    delete the dead git status check that never looks at its result) (kills C6).
- Dead-code cleanup: `Candidate.parent`, `Report.log/rounds/floors`, `eval.py:272-273`;
  the critic `n>1` multi-reviewer path stays (phase 2 adversarial re-review will use it), with a comment saying so.
- **Verification**: `python3 -m unittest` all green + fixture E2E + two concurrent profile runs that do not clobber each other.

### P5: optional items (listed separately, each decided on its own)

- **Delete the parallel chart stack** (B6, open question Q2): delete `trajectory.py` + `chart.svg/ascii_chart`
  + the `aro chart` subcommand (the real reports use `perf_token_svg`/`explore_svg` and are unaffected). About -500 lines.
- **Resource-based prompt paths**: `importlib.resources` first, repo-layout fallback, so the package is genuinely installable and relocatable (finishes D2).
- **Typed events**: upgrade the runlog constants table to lightweight dataclasses (diminishing returns, do it last).
- viz build artifacts: keep committing them (a pragmatic choice for zero-dependency distribution); only add a CI check that the
  template is in sync with `viz/src`; no LFS.

---

## 4. Runtime optimization opportunities in ARO itself (judging throughput = the bottleneck you named yourselves)

The infinite-flow design §2.4 states plainly that serial judge throughput is the only bottleneck. The following four items buy back
wall clock directly; do them alongside P3/P4 or right after:

1. **Eliminate the prescreen→evaluate double build** (the biggest item): for a candidate that passes prescreen, `prescreen` has
   already done apply+build in the `pre-<id>` worktree (`eval.py:110-122`), yet `evaluate` opens a fresh
   `cand-<id>` worktree and builds from scratch (`eval.py:170`). A full Rust build takes minutes:
   **every surviving candidate pays for one extra build**. Fix: on prescreen success, return and keep the worktree,
   and let evaluate reuse it (same candidate, same td, so the "different code never shares a td" invariant holds;
   the forced-recompile self-check keeps its meaning, because that worktree just compiled this exact candidate anyway).
2. **Cache the prescreen baseline smoke bench per round**: today every candidate re-measures the baseline
   (`eval.py:129`); a round of N candidates = N redundant baseline benches; the baseline does not change within a round, so measure once.
3. **Cache noise floors keyed on (scale, baseline state)**: auto-tighten reruns
   `calibrate_floors` (2×aa_runs benches) every time it raises scale; the same scale can be reused while the baseline
   has not advanced, and is invalidated for recalibration after a baseline fold.
4. **sccache experiment** (optional): `RUSTC_WRAPPER=sccache` keeps the per-worktree td isolation
   (the invariant holds) while the compiler-level cache cuts recompile time. Before enabling, an A/A run must confirm the floors are unchanged.

---

## 5. Risks and the most fragile assumption

- **Most fragile assumption**: "selftest green = behavior unbroken". It only covers pure logic; it sees none of the real
  cargo/git/claude paths in `target.py`/`generator.py`. **The plan is already shaped around this**:
  the P1 cargo fixture E2E goes first, and every P2/P3 commit passes it plus the key-artifact diff
  (same events.jsonl input → byte-identical manifest/tree output).
  If the fixture E2E cannot be built (for example, installing Rust in CI is restricted), then the P3 `target.py` changes
  degrade to rename-only, with no implementation changes.
- **The git-timeout fix is itself a risk**: after adding timeouts to worktree operations, a value that is too small can
  kill healthy runs on slow disks; use the same value as `spec.timeout`, consistent with cargo.
- **Rollback**: everything is a code refactor with no data migration and no external state; any phase reverts with `git revert`.
  The event contract is add-only, so old run directories can always be rendered by new code.
- **Honest note on scale**: P2+P3 touch about 20 files, above the 8-file threshold; the blast radius is contained by
  "6 independent commits per phase, each passing the full verification set".
- **Dependency list**: no new runtime dependencies; CI needs GitHub Actions (the repo is already on GitHub) and the
  Rust toolchain (installed inside job 2); no secrets or credentials needed; the `claude` CLI does not enter CI.

---

## 6. Open questions (your call)

| # | Question | My recommendation |
|---|---|---|
| Q1 | `memory/lessons.jsonl`: keep it git-tracked (status quo, diff noise after every run) or untrack it? | **Keep it tracked** (it is cross-run memory, part of the product), but commit it in a dedicated commit at run wrap-up so it never sits dirty for long |
| Q2 | Parallel chart stack (`trajectory.py` + `aro chart`): delete or keep? | **Delete** (the real reports do not use it; -500 lines) |
| Q3 | Generated-report language: unify on English or keep the Chinese/English mix? | **English** (matches the English-only skill policy; Chinese stays in the design docs) |
| Q4 | What to do with `remote-readme.md` (the unrelated host inventory)? | **Move it out of the repo** (do not paper over it with gitignore; move it out directly) |
| Q5 | Make it pip-installable? | **Yes** (P1 lays the base, P5 finishes it), while keeping clone-and-run working |
| Q6 | The four runtime optimizations (§4): do them with P3/P4, or as a separate round after the refactor settles? | **Do §4.1 (double build) with P3** (largest payoff, and it shares territory with the eval/target interface changes); the rest as a separate round |

---

## 7. Execution order and size

```
P0 hygiene(0.5d) → P1 safety net(1.5d) → P2 dedup(2.5d) → P3 split(4d) → P4 tests+robustness(2.5d) → P5 optional
```

Total about 11 person-day equivalents (agent-driven runs faster in practice). P0/P1 change no behavior; from P2 on, every commit
passes the triple gate of selftest + fixture E2E + artifact diff. Stop at any phase and the repo is in a better
usable state than before (phases merge independently).

**Minimal plan** (if you only want a third of it): P0 + P1 + P2: no structural changes, just the safety net, the
three correctness risks (A1/A2/A3) killed, and the git timeout gap closed, about 4.5 person-days.
