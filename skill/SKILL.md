---
name: aro
description: Autonomously optimize performance-critical code and prove the win is real. Profiles the real hot path, makes ONE behaviour-preserving (byte-identical) change, and scores it with a deterministic judge (A/A noise floor + paired A/B + bootstrap CI + random-input differential) that can't be fooled or gamed on a sub-1% change buried in benchmark noise. Use when asked to make a crate/repo faster without changing behaviour, find or fix a real performance bottleneck, reproduce or verify that a perf optimization isn't noise, set up a new optimization target (free-form goal → spec, with a dry-run), run an unattended / overnight optimization loop on a repo that has no spec yet (the agent writes its own probe and verifies), or render a run report from a run's events.jsonl. Targets Rust / cargo today; generalizes to a new target via a spec, not code.
---

# ARO: autonomous performance optimization

Profile the real hot path, make ONE behaviour-preserving change, and score it with a
deterministic judge that can't be gamed on a sub-1% change buried in noise. **The loop is
commodity; the judge is the moat.** A new target is one `targets/<name>.json`: a spec, not code.

## Subcommands

| command | purpose |
|---|---|
| `python3 -m aro plan "<goal>" <repo>` | free-form goal → validated 7-slot spec (detect → agent fills slots + writes probes → dry-run → slot dump) |
| `python3 -m aro sweep <spec.json>` | frontier map (L1, report-only): profile → bucket ours/not-ours → cross-ref lessons → the actionable untried hot functions |
| `python3 -m aro sweep <spec.json> --attempt` | L3 unattended: walk the frontier heaviest-first, run the full judge on each hot fn, compound accepts, re-profile on top, until exhausted / budget. `--probe-factory` enables the L4a probe factory (defaults ON under `--diverge`); `--workloads N` runs the L4b synthetic-workload campaign |
| `python3 -m aro run <spec.json>` | run the full loop (L2: propose one judged change) on a target spec |
| `python3 -m aro run <spec.json> --blind` | same, profiler-only hint (no technique named): honest blind-discovery |
| `python3 -m aro tree <out-dir>` | (re)render the report (`decision-tree.html` + `tree.json`) from a run's `events.jsonl`, no re-run |
| `python3 -m aro manifest <out-dir>` | the final accepted edit-set + provenance + `mergeable` flag (`manifest.json`): the hand-off to turn a run into a PR (`references/run-data.md`) |
| `python3 -m aro union [specs…]` | cross-campaign view over permtree ledgers: workload lanes, per-fn judgment matrix, compounded wins, open measurement debt (`union-report.html` + `.json`) |
| `python3 -m aro serve <out-dir> [--port 8010]` | serve the report over HTTP (live-refreshes from `events.jsonl`) for headless server runs; binds 127.0.0.1 by default, pass `--host 0.0.0.0` explicitly to expose it (unauthenticated) |
| `python3 -m aro clean <spec.json> [--dry-run] [--registered] [--runs DIR]` | remove a spec's orphaned worktrees + per-worktree target dirs (git-registered ones kept unless `--registered`); `--runs DIR` also removes run dirs no permanent ledger references (referenced runs = the audit chain, always kept). Explicit command by design — never runs in the background |
| `python3 -m aro recheck <spec.json> [--ref REF]` | the computed re-run signal: diff the pinned baseline against the head; churn under the editable regions → RE-RUN (re-pin baseline, re-derive DIFF, L1 first), untouched regions → the campaign's claim stands. Answers "should we run again?" with a computation, not a feeling |
| `python3 -m aro coverage <spec.json>` | dark-region report (needs cargo-llvm-cov): run every registered workload probe instrumented into one merged profile; workspace functions that never executed land in `targets/<spec>.coverage-gap.json`, where the workload factory's author prompt picks them up as named targets. The honest footnote on any exhaustion claim |
| `python3 -m aro hotpath <spec.json>` | observe only: profile + isolated-kernel latency, no changes (root `find_hotpath.py` is a thin shim over this) |
| `python3 -m aro verify-patch <patch> --spec <spec.json>` | re-score a recorded patch through the full judge (root `verify_patch.py` is a thin shim over this) |
| `python3 selftest.py` | cargo-free self-test (compounding + event log) |

## The loop (one round)

`observe → read → generate → judge → record → reflect → (goal met / dry? → stop)`

Profile the baseline → a READ-ONLY plan for one byte-identical change → a write-compile-fix
candidate in a throwaway worktree → the **judge** verifies correctness then significance →
record + compound the accepted patch into the working baseline → reflect into the next round's
research agenda. Generation is swappable (the spec's `generator` slot: `agentic` / `ralph`);
the judge is identical either way.

## Routing: which doc for what

| you are… | read |
|---|---|
| bringing ARO up on a NEW MACHINE, or the frontier map collapsed (empty / one bogus fn / `source not located`) | `references/new-box-checklist.md` |
| adding a NEW TARGET end to end (plan → probes → six-leg dry-run → review gates → launch; repo-shape gotchas) | `references/add-a-target.md` |
| setting up a new target (free-form goal → validated spec, dry-run) | `references/plan-workflow.md` |
| writing the probe or the differential (isolate the kernel, prove byte-identical, adversarial corpus) | `references/harness-protocol.md` |
| deciding **what** change to make (the eliminate / weaken / codegen lens + the adoption rule) | `references/optimization-lenses.md` |
| what is a **bad** optimization even when it's faster (maintainability filter + worked examples; the layer-preserving variant) | `references/optimization-examples.md` |
| understanding how scoring works (the gates, A/A floor, paired A/B, bootstrap CI, measurement self-checks) | `references/judge-protocol.md` |
| mapping the whole frontier (what's our lever vs untouchable, what's tried, what's left): the meta-loop that converges to a map | `references/sweep-protocol.md` |
| running unattended with **no spec** (agent writes its own probe + verifies) | `references/autonomous-optimization.md` |
| filling the spec slots | `references/spec-slots.md` |
| the persisted state / event-log vocabulary | `references/results-logging.md` |
| **consuming a run's data**: where every file/field is, what it means, and how to turn a run into a PR (read `manifest.json`; accepted ≠ should-merge) | `references/run-data.md` |
| **opening a PR from a run**: apply ONLY `mergeable:true` edits, build+test gate, PR body from the manifest; 🟡 wins go to a human, never auto-PR/auto-merge | `references/run-to-pr.md` |
| **judging the non-mergeable accepts yourself** (delegated review: read diff + critic reasons, verify on current main, per-edit verdict, PRs or rejection with a written report) | `references/evaluate-run.md` |
| the rules for ANY pr built from a run (decide-first grouping, coverage + mutation test gates, number provenance, violation-grade rails) | `references/pr-discipline.md` |
| **reporting a run as a Lark/Feishu card**: `aro manifest`/`tree` → a card JSON 2.0 (skeleton + gold example), incl. uploading `perf-token.png` for the chart | `references/lark-card.md` |
| rendering `RUN-REPORT.md` from a run's `events.jsonl` | `references/report-protocol.md` |
| writing the human **daily optimization report** for a round (what changed / how much it improved / what code changed / what to do next, plus the regime decisions for a human) | `references/daily-report-protocol.md` (+ `daily-report-template.md`) |
| the deeper "why" behind the rules + the honest limitations | `references/core-principles.md` |

Two folders: `references/*.md` are prose docs you read to understand the system; `prompts/*.md`
are the **executed** templates (`$placeholder` substitution) ARO feeds the model; `aro/prompts.py`
loads them, a spec's `prompts` slot names them. Prompts embed only the minimal rules; the long
rationale lives in the references above.

## Non-negotiables (the moat in one breath)

1. **The writer never grades itself.** A separate, deterministic evaluator (`aro/{eval,stats,guard}.py`)
   scores every candidate; "looks faster" is banned.
2. **Behaviour stays byte-identical.** The correctness gate (build + test + random-input
   differential vs a *frozen* baseline) runs before significance; the candidate edits
   implementation source only, never `Cargo.toml`/`Cargo.lock`, `benches/`, or `tests/`.
3. **A win counts only if** the measured Δ clears the A/A-calibrated noise floor **and** a
   bootstrap CI that excludes 0. Direction-aware per objective.
4. **The report is a view of the event log, never a re-judgement**: every number copied
   verbatim, a within-noise / regressed result never laundered into a win.

The full rule set, domain table, and honest limitations are in `references/core-principles.md`.
