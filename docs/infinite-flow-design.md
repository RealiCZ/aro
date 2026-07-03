# ARO Infinite-Flow Deep Search: Design Proposal (v1, pending review)

*Status: historical decision record. Phase 1 shipped; Phase 2 partially superseded by docs/self-extending-search-design.md.*

> Upgrade the explorer from "greedy, one round and move on" to "**parallel multi-agent deep search that runs to completion and outputs a full decision tree**", making the search deeper, wider, and parallel while **leaving the judge (the moat) untouched**. This document covers principles, architecture, per-item mechanisms, invariants, costs, CLI, and rollout phases, and ends with the **open questions that need your sign-off**.

---

## 0. Background: why change

Today each hot function gets only **1 candidate**, and anything within noise moves on (`rounds_per_fn=1`, `dry_rounds=1`). The pile of **purple "untried"** nodes in the decision tree (the d1/d2/d3 directions that agent reflect proposed but never got a turn) are real ideas cut off by this shallow search. Conclusion (from the unlimited-compute discussion): **search is a commodity, the judge is the moat; what should loosen is the "give-up threshold", what must stay locked down is the judge plus coverage.** So open up the search and hold the judge.

---

## 1. Goals / non-goals

**Goals**
- **Depth**: multiple rounds with reflect fed back into the next round; a function is only judged infeasible after several failed rounds in a row (no more "estimate once and give up").
- **Parallelism**: multiple agents **generate diverse candidates in parallel** (different lenses / framings).
- **Exhaustiveness**: walk the full frontier and upgrade tried/gated; do not shrink at the 3rd dry round.
- **Visibility**: **automatically** produce `decision-tree.html` when the run finishes.
- **Autonomy**: fully automatic within-regime; a human signs off only at true regime gates.

**Non-goals (hold these, do not touch)**
- The full judge (Gate 0/1/2), crypto/base untouchable, generality (no special cases, use cargo metadata), and **bench measurement integrity**.

---

## 2. Core principles (the three that decide the architecture)

1. **The judge is the moat, search is a commodity** → open up the search, lock down the judge.
2. **Generate in parallel, score serially.** Generation (writing candidates) can be parallelized freely; **bench must be serial**. Parallel benches fight over CPU/cache and trigger frequency throttling → the noise floor explodes → no win can be proven = **the moat is destroyed**. This is the root of the architecture.
3. **Only two true regime decisions keep a human gate**: (1) relaxing the oracle (accepting should-not-merge), (2) changing the workload (needs domain judgment on what counts as representative). Everything else (switching functions / climbing lenses / more rounds / re-profile / stacking) is autonomous.
4. **[Unlimited tokens → the bottleneck is the judge.]** Since generation costs nothing, fan-out can be unlimited; but every candidate has to queue through **that one serial bench**. **Serial judge throughput therefore becomes the only bottleneck**, which again confirms that the judge is the load-bearing wall. Direct consequence (see §4.3b): a **cheap prescreen plus scoring-queue priorities** are mandatory, otherwise an unlimited stream of junk candidates drowns the scarce serial scoring. **"Unlimited" means unlimited generation, not unlimited scoring; wall-clock is set by the serial judge.**

---

## 3. Architecture

```
                    ┌─ agent(lens=remove redundancy) ──┐
  frontier queue →  ├─ agent(lens=data layout) ────────┤   parallel fan-out generation
 (function × lens   ├─ agent(lens=algorithm rewrite) ──┤   wall-clock = time of 1 agent
  × reflect)        └─ agent(framing=risk-first) ──────┘   → N candidate patches
                                                      │
                                                      ▼
                            ┌────── single serial scoring queue ──────┐
                            │  per candidate: Gate0 guard → Gate1     │   isolated worktree
                            │  correctness (build+test+differential)  │   bench never parallel
                            │  → Gate2 significance                   │
                            │  (A/A+A/B+CI+floor+auto-tighten)        │
                            └────────────────┬────────────────────────┘
                                             ▼
                       judge picks the best (direction-aware) → accept?
                          accept → stack onto baseline → re-profile → back to frontier
                          all dry → upgrade tried/gated → STOP when exhausted
                                             ▼
                          wrap-up: decision-tree.html + trajectory.png
```

- **Concurrency lives only in generation** (a thread pool inside the aro process spawns `claude -p`, not a harness subagent → no hang problem).
- **Scoring is a single consumer** (measurement cleanliness); Phase 2 upgrades it to producer-consumer.

---

## 4. Mechanism design, item by item

### 4.1 Depth: multiple rounds + reflect feedback + lens ladder (Phase 1)
- `rounds_per_fn` 1 → **4~6**; per-fn `dry_rounds` 1 → **3**. run_backtest already feeds the agenda (reflect directions) into the next round's `GenContext`; just raise the round count and d1/d2/d3 will **actually be tried one by one**.
- **Lens ladder**: `lens_depth = f(dry rounds so far for this function)`, injected into the prompt: round 1 micro-elimination → if it fails, climb to data layout → if that fails too, climb to algorithm level.
- Touch points: `spec.run.stop` / `--rounds-per-fn`; `generator.py` + `prompts/agentic.md` (add `$lens_depth`).

### 4.2 Parallel multi-agent generation (Phase 1, the core)
- `AgenticGenerator.propose(ctx, N)`: use `ThreadPoolExecutor` to **spawn N `claude -p` processes concurrently**, each with a prompt for a **different lens / framing** → collect N candidate patches.
- Concurrency cap (default `min(N, 8)`); if a single agent dies → drop that candidate without affecting the rest (the `.filter(Boolean)` idea).
- The engine still **scores these N serially**; the judge picks the direction-aware best.
- Wall clock: drops from N × agent to ≈ 1 × agent (the slowest link is parallelized).
- Touch points: `propose` in `generator.py`; the engine already supports `candidates_per_round=N`.

### 4.3 Serial scoring (unchanged, can be strengthened)
- Each candidate goes through Gate0/1/2 **serially**, in an isolated worktree; bench is never parallel (invariant, see §6).
- With unlimited tokens: turn on **higher `bench_scales` / more `ab_pairs` / more `aa_runs`** by default → the floor is pushed extremely low, `noise-limited` almost disappears, and small wins become distinguishable; every accept then goes through **adversarial re-review** (§4.7).

### 4.3b Cheap prescreen + scoring-queue priority (Phase 1; a new must-do under unlimited tokens)
> Unlimited generation → the serial judge is the bottleneck → **junk candidates must not waste the serial bench**. Add a **cheap gate** before the expensive A/A+A/B and give the scoring queue priorities.
- **Cheap prescreen (seconds, parallelizable)**: (1) does it build? (2) does the patch **actually differ** from baseline (pure reformatting / equivalent change → drop); (3) a **one-shot quick smoke bench** (single run, low sample count) for a rough Δ estimate. Fail any of the three → the candidate does not enter the serial scoring queue.
- **Dedup**: **dedup** the N generated candidates by "which lines changed / AST shape"; equivalent candidates are scored only once.
- **Priority**: the serial bench queue scores in descending smoke-Δ order: the most likely winners go first, so scarce scoring time is not wasted on hopeless candidates.
- Touch points: add a prescreen before scoring in `engine.py`; dedup in `generator` after candidates are produced.

### 4.4 Exhaust the frontier (no early stop) (Phase 1)
- `_explore_decision`: **remove "stop across functions when `dry_streak≥3`"** (that was cost-saving logic). Replace with: walk the full frontier → upgrade tried/gated → stop only at **headroom ≤ threshold** (truly nothing reachable) or **budget cap**.
- Per-fn: only judge a function infeasible after exhausting the lens levels and reflect directions.
- Touch points: `_explore_decision` in `sweep.py` / the `attempt` main loop.

### 4.5 Auto-produce the decision tree at wrap-up (Phase 1)
- In the wrap-up of the `--attempt` branch of `sweep.main`: call `tree.build_tree` → write `decision-tree.html`; `trajectory.svg → trajectory.png`.
- Touch points: the last few lines of `main()` in `sweep.py`.

### 4.6 Cross-function parallelism + a single serial bench queue (producer-consumer) (Phase 2)
- Feed the whole frontier's (function × lens × reflect) into the agent pool for **parallel generation**; all candidates converge into **the same single serial bench queue** for scoring. This is the complete form of true infinite flow. A big change; listed on its own.

### 4.7 Dual regime / adversarial re-review / automatic multi-workload (Phase 2)
- **Dual regime**: `--allow-relaxed` switch; automated runs still mark it gated, and relaxed wins are labeled should-not-merge.
- **Adversarial re-review**: after an accept, fan out N skeptic agents to re-verify (re-bench / re-differential / argue the opposite); the accept only stands if it survives.
- **Automatic multi-workload**: synthesize a batch of workloads covering different behavior paths and profile their union. **The biggest item; recommend a separate project.**

---

## 5. Stop conditions (updated)

**Unlimited tokens → only two true stop conditions remain (budget is no longer a reason):**

| Trigger | Threshold | Notes |
|---|---|---|
| **Addressable exhaustion** | `addressable headroom ≤ headroom_min` (default 2%) | no own, locatable, hot-enough functions left |
| **True exhaustion** | every function × lens × reflect has been judged, no new wins | the whole decision tree has been walked |
| ~~Budget cap~~ | (off by default) | unlimited tokens → dropped; `--max-attempts` remains only as an **optional safety valve**, unlimited by default |

> The key change: **stopping moves from "diminishing returns on cost (dry_streak≥3 stops to save money)" to "provable exhaustion"**. Infinite flow walks the whole tree and stops only when there is truly nothing left to hit.

---

## 6. Invariants (no change may break these)

1. **Bench is serial**: measurement is never parallelized.
2. **The writer never grades itself**: Gate0 guard; never edits `Cargo.toml`/lock, `benches/`, `tests/`.
3. **Correctness before significance**: build + test + random-input differential must all pass before A/B is measured.
4. **Numbers verbatim**: reports/tree read only events.jsonl; no re-scoring.
5. **Generality**: owner/location go through cargo metadata; no piles of special cases.

---

## 7. Costs and trade-offs (unlimited-token edition)

- **Tokens are not a constraint** (per your call): fan out generation freely. **The real bottleneck = serial judge throughput** (§2.4). So the prescreen/dedup/priority in §4.3b is **not an optimization, it is mandatory**: it decides where the scarce serial bench time goes.
- **Wall-clock**: set by "number of candidates entering the serial queue × time per bench". The prescreen keeps most junk out of the queue → wall-clock stays under control even with unlimited generation.
- **Risk**: deep search climbing to algorithm level → more structural changes → more `verify-failed`/`build-failed`, but **the judge absorbs all of it**; conclusions are not polluted.
- **CPU**: generation (waiting on network/IO) does not take bench resources; scoring is serial and exclusive, so measurement stays clean.

---

## 8. Config / CLI (new/changed)

| Parameter | Default (unlimited tokens) | Purpose |
|---|---|---|
| `--exhaustive` | **on (default)** | exhaust the frontier; remove the cost-saving dry-stop |
| `--fanout N` | **large (one per lens × framing)** | candidates generated in parallel per round; unlimited tokens → fill it out |
| `--gen-concurrency` | **16** | concurrency cap for generation agents (pure network/IO) |
| `--rounds-per-fn` | **unlimited** (until exhaustion) | rounds per function; reflect feedback deepens each round |
| `--dry-rounds` | **3** | consecutive rounds with no new direction before a function counts as exhausted |
| `--prescreen` | **on** | the §4.3b cheap prescreen (build+differs+smoke) + dedup + queue priority |
| `--max-attempts` | **unlimited** (optional safety valve) | set only if you want a backstop |
| `--allow-relaxed` | off | open the relaxed regime (should-not-merge); **gated for correctness, not cost**, so still off by default |

---

## 9. Rollout phases

- **Phase 1 ✅ landed (this round)**: 4.1 depth (lens ladder micro→layout→algorithm + rounds_per_fn 4 + per-fn dry 3) + 4.2 parallel generation (`AgenticGenerator/RalphGenerator` thread-pool fan-out of N candidates with different lenses, capped by `--gen-concurrency`, each candidate with its own worktree/CARGO_TARGET_DIR, no id collisions) + 4.3b prescreen (`eval.dedup_candidates` dedup + `eval.prescreen` build+smoke cheap gate + `engine` scoring queue sorted by smoke-Δ; dropped candidates are still recorded, not silent) + 4.4 exhaustion (`_explore_decision(exhaustive=True)` removes the cost-saving dry-stop, leaving only headroom exhaustion + true frontier exhaustion; `--max-attempts` becomes an optional safety valve) + 4.5 automatic decision tree (`_finalize_run` produces `decision-tree.html` + `trajectory.png` at wrap-up). Covered by selftest #21; all 21 cases pass.
  - CLI: `aro sweep <spec> --attempt --diverge [--fanout N] [--gen-concurrency N] [--prescreen/--no-prescreen] [--exhaustive/--no-exhaustive] [--dry-rounds N] [--rounds-per-fn N]`.
- **Phase 2**: 4.6 fully async producer-consumer (cross-function parallel generation → one serial bench queue) + 4.7 dual regime / adversarial re-review.
- **Separate project**: 4.7 automatic multi-workload (the coverage axis).
- **Interleaved supporting work**: the example library `optimization-examples.md`, expanding region to direct callees, doc readability.

---

## 10. Open questions pending your review

> Already decided: **unlimited tokens** → (1) fill out the fanout, (2) `--exhaustive` on by default, (3) drop the budget stop (`--max-attempts` only as an optional safety valve), (4) add the §4.3b cheap prescreen (mandatory because the judge is the bottleneck).

**Still needs your call:**
1. Do the **§4.3b prescreen** this round (I think it is required, otherwise unlimited candidates drown the serial judge). Agree to fold it into Phase 1?
2. Are **`dry_rounds=3` / 3 lens levels** reasonable as the criterion for "exhausted"? (Exhausted = 3 consecutive rounds where reflect proposes no new direction.)
3. **Expanding region to direct callees** (deep structural changes need to cross a few callees in the same crate): how loose or tight? (Never open Cargo/bench/test.)
4. **Dual regime (`--allow-relaxed`) + adversarial re-review**: this round, or leave for Phase 2? (Note: these are gated for **correctness**, not cost. Even with unlimited tokens, relaxing the oracle still changes the "kind of win", so keep it as human-gated opt-in.)
5. Confirm **automatic multi-workload** as a separate project?
6. Is a **generation concurrency cap of 16** enough? (How many concurrent `claude -p` processes can the machine/network sustain?)
