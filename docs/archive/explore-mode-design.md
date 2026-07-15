# ARO Explore Mode: Design Proposal (pending review)

> **Historical design document** — may not reflect the current system. See [OPERATIONS.md](../OPERATIONS.md) and [ONBOARDING.md](../ONBOARDING.md) for what ships today.


*Status: historical decision record. Superseded by docs/archive/infinite-flow-design.md and docs/archive/self-extending-search-design.md.*

> Upgrade ARO from "prove one mergeable win" to "**unlimited attempts, every route drawn as a tree, humans decide only at the leaves**".
> The model casts a wide net across the whole project while keeping the measurement rigor, and produces a tiered attempt tree; a human decides "merge / do not merge / add an example" at the leaves, and the examples in turn prune later exploration. **Exhaustion is not the goal, but every rough direction is guaranteed to be reachable.**

---

## 0. What this is and what it is not

- **Is**: an unlimited explorer + one reviewable tree of "every route tried" + humans deciding at the leaves + an example library that grows with feedback.
- **Is not**: (1) it does not produce "directly mergeable" versions (whether to merge, and whether it should be done this way, **is decided by a human at the leaf**); (2) it does **not aim for exhaustion** (unlimited generation meets finite serial scoring; it cannot be done); (3) but **rough directions must be reachable** (it must not keep digging only the fattest spot while other directions never get a turn).

---

## 1. What is kept (the moat core, not loosened at all)

1. **A/A + A/B significance**: A/A noise floor + paired A/B (alternating order) + bootstrap CI excluding 0 + floor check + auto-tighten. **Bench is always serial.**
2. **Correctness floor**: build + cargo test must pass.
3. **A real measured win**: there must be a **significant** speedup on a bench (existing or newly written), proven by A/A+A/B.
4. **Gate 0 anti-cheat red lines**: the writer never touches the ruler: never edits `Cargo.toml`/lock, `benches/`, `tests/`.
5. **★ Independent semantic review (critic gate)**: **every new artifact (bench, code, idea) must pass an independent review subagent before it counts.** The deterministic judge proves the "numbers" (fast + correct), but it cannot judge "was this bench rigged / is this code a reward-hack / does this idea hold". That gap is filled by an independent LLM review (adversarial, skeptical by default). **Both gates must pass before anything enters the tree.** (See §3.0.)
6. **★ Full traceability (audit trail)**: both judges record **not just "pass/fail" but "why"**: for every joint node (idea/plan, bench, each candidate, each gate, each review, the final verdict), the **verdict content + reasons + numbers** go verbatim into events.jsonl and are assembled at the tree's leaves into a complete "why it passed / why it did not" chain. **Every conclusion can be traced back to evidence.** (See §3.7.)

---

## 2. What is relaxed (traded for exploration breadth + fewer human gates)

| Dimension | Strict mode | Explore mode | Cost (accepted knowingly) |
|---|---|---|---|
| Correctness oracle | byte-identical differential | **passing unit tests is the floor**; changes to code with **no test coverage** → **auto-generate a differential as a backstop** (the EVM never runs bare) | accept ≠ byte-identical proof |
| Scope | the single located file | **the src of the whole workspace** (Gate 0 red lines still apply) | more large cross-crate changes |
| Maintainability gate | human gate on every change | **example-driven** (soft guidance + hard pruning); humans only decide at the leaves / add examples | the example library needs upkeep |
| Workload | human-written spec | existing benches, or **agent-written** (maker-checker) | microbenchmark representativeness is questionable |
| Output semantics | accepted = provably mergeable | accepted = **a lead, for a human to settle at the leaf** | humans do the final triage |

---

## 3. Mechanisms

### 3.0 ★ Independent semantic review (critic gate): two judges
maker-checker extends from "numbers" to "semantics": besides the deterministic judge, every new artifact must also pass an **independent review subagent** (fed a review prompt), and **only then does it count**. Each of the three artifact types has its own rubric:
- **bench**: does it actually drive the target function? Is it trivial / easy to "game" for a win? Is the isolation right? (Guards against cheating by writing your own ruler.)
- **code**: a real optimization, or a **reward-hack / bench loophole**? Does it hit a **known bad pattern** (`optimization-examples.md`, e.g. dissolving the layering)? Are cross-boundary / weak-oracle risks flagged?
- **ideas** (read-phase plan + candidate hypothesis): does the reasoning hold? Are the invariants it relies on real?

**Where it goes and why it can be trusted (the key part)**:
- **Placed before the serial judge**: reviews are parallel LLM calls (parallel like generation); **only candidates that pass review enter the scarce serial bench**. It guards semantics and doubles as a smart prescreen that saves throughput. It is a stronger version of the §3.4 hard example pruning.
- **Independent + adversarial**: the reviewing agent ≠ the generating agent (maker-checker); the prompt tells it to be **skeptical by default and actively look for problems** ("try to prove this is wrong / gamed / should not be done"); when unsure, **reject**.
- **Multiple votes** (interface reserved, not built now): a **single reviewer** is enough for now; the interface is designed as "N reviewers vote independently, majority passes" so it can slot in smoothly (a reviewer is just a `critique(artifact) -> {verdict, reasons}` function; N votes is just more calls plus a majority). **Single first, multiple later.**
- **No watering down**: the review is an **extra** gate (AND, not OR); a candidate must pass **both** the deterministic judge and the review. The review can reject, but it cannot overturn the "numbers".
- **Auditable**: every review's "pass/fail + reasons" is **recorded in events.jsonl and shown on the tree's leaves**; a human can see "why it was rejected" and **add an example** based on it (feeding back into §3.4).
- **Who reviews the reviewer?** The reviewer is itself an LLM and will make mistakes. Mitigations: (1) independence + adversarial default-reject; (2) multiple votes; (3) reasons go verbatim onto the tree, and a human does the final check at the leaves. **The review is a second judge, not the truth.**

### 3.1 Generation is unlimited, scoring is serial (throughput is the real ceiling)
Generation can fan out without limit; but every candidate must pass the one **serial bench**. Wall clock = candidates entering the serial queue × time per bench. So these are mandatory:
- **Cheap prescreen**: does it build? Does it actually differ from baseline? One smoke bench for a rough Δ. Fail any of the three → it does not enter the serial queue.
- **Dedup**: equivalent changes (same AST / same text) are scored once.
- **Scoring-queue priority**: score in descending smoke-Δ order; scarce scoring time goes first to the most likely winners.
> "Unlimited" = unlimited generation, best-effort prioritized scoring; **not unlimited scoring**.

### 3.2 Scope = the whole workspace
`editable` widens to the `src/` of all workspace members; the Gate 0 red lines (Cargo/lock, benches, tests) do not change. Breakage is caught by build+test+(differential).

### 3.3 Self-written benches + maker-checker + no-coverage backstop
- The changed code has no usable bench → **autonomously write a per-function microbenchmark**.
- **maker-checker**: the agent that writes the bench ≠ the agent that writes the optimization; the bench is **frozen first**, then optimized against (guards against cheating by writing your own ruler).
- Changing a function with **no test coverage** → **auto-generate a differential probe** (random inputs, compare fingerprints); otherwise "unit tests pass" is empty.

### 3.4 Examples: soft guidance + hard pruning
- **Soft** (generation side): bad-practice examples from `optimization-examples.md` (e.g. PR#313's double `inspect_storage` dissolving the layering) are injected into the prompt.
- **Hard** (before scoring): a candidate similar to a "known bad pattern" → **automatically demoted to the back of the queue / skipped**; no serial scoring wasted on it.
- **The loop**: a human says "this should not be done" at a leaf → a new example lands → future runs avoid it automatically. **This is where "reducing the burden" actually takes effect.**

### 3.5 ★ Direction reachability: breadth first + explore/exploit budget (the key new mechanism in this proposal)
Pure greedy by smoke-Δ keeps digging only the fattest spot (e.g. the storage path); precompiles / resource limits / dual-gas and the like **never get a turn**, which violates "rough directions must be reachable". So:
- **Breadth before depth**: on the first pass, take **one shot per crate / per direction class** (breadth) to find where there is promise; then deepen where there is.
- **Explore/exploit quota (bandit / MCTS style)**: the scoring queue is **not purely greedy**; a share of the budget is reserved for the **least-explored directions** (new crates, new techniques, new tiers) even when their smoke-Δ is unknown. This way **every major direction gets attempt slots**, with no need for exhaustion.
- **Coverage axes (4 core ones, already decided)**: the quota is spread along these axes to keep the search from feeding on one thing only:
  1. **crate**: do not dig only one crate (mega-evm / ipa-multipoint / banderwagon / salt-glue and so on, via cargo metadata).
  2. **technique (the most important)**: do not just change the lens tier, change the **approach**: removing redundancy / caching and memoization / data layout / removing allocations / batching and fusion / algorithm replacement / branch reordering. This axis guarantees **different kinds** of changes are tried, not different tiers of the same kind.
  3. **heat tier**: top >5% / middle 1-5% / tail <1%; do not stare only at the #1 hotspot.
  4. **risk tier**: byte-identical / relaxed and structural / cross-crate; both safe wins and bold exploration must be present.
  - Optional finer axes: **source** (own crate / editable fork / glue), **module** (finer than crate).
> This is how "not exhaustive but directions reachable" lands: **spread the explore/exploit quota along these axes to guarantee coverage, instead of walking the whole tree.**

### 3.6 Output: a tiered reviewable tree (this is decision-tree.html)
Unlimited exploration → thousands of leaves; a human cannot review them all. So the tree is **tiered**:
- **Top: the N worth your attention**: big wins first, flagged with **risk** (cross-crate / dissolved layering / hits a bad example / weak oracle).
- **Long tail: similar attempts clustered and folded.**
- Opening a leaf shows: the **complete "why" chain** (see §3.7) + the diff (compact, colored) + Δ/CI + maintainability flags, so a human can make the "merge / reject / add an example" decision **in seconds**.

### 3.7 ★ Full traceability: every joint node records "verdict + reasons"
Both judges leave the **verdict content** on record, assembled into a complete evidence chain at the leaf. The traceable path of one candidate:
1. **The idea / read-phase plan** + the review's "pass/fail + reasons" on the idea.
2. **The bench** (if newly written) + the review's "pass/fail + reasons" on the bench + the bench's own profile (proving it actually drives the target function).
3. **The code candidate** + the review's "pass/fail + reasons" on the code (which bad example it hits, reward-hack suspicion, and so on).
4. **Each deterministic gate**: guard / build / test / differential / significance, with each one's **status + detail + numbers** (Δ/CI/floor/scale, auto-tighten records).
5. **The final verdict** + a one-line attribution.
- All of it goes **verbatim into events.jsonl** (the machine truth); the tree **only reads, it never re-scores** (invariant §6.4).
- Review reasons are recorded **in structured form** (verdict / rubric items hit / key arguments), not a blob of free text, so the tree can display them and a human can quote them directly when adding examples later.
- Value: (1) a human understands **why in seconds** at the leaf; (2) rejected candidates can be traced and turned into **counterexamples**; (3) when things go wrong (a review kills or passes wrongly), the audit can reach **the specific reason** after the fact.

---

## 4. The human loop (leaves only)
For each candidate that surfaces, a human picks one of three: **(1) adopt (into the persistent baseline), (2) reject, (3) add an example** (reject plus an explanation of why). None of the three interrupts exploration; (3) feeds back into 3.4 to prune future runs.

---

## 5. Stopping (not exhaustive)
- **Budget**: tokens / wall clock (with unlimited tokens, wall clock is set by serial scoring).
- **Diminishing returns**: a direction with K consecutive rounds of no new wins → downweight that direction (but the §3.5 quota still guarantees it **was explored**; it just stops being dug deeper).
- **Direction reachability is guaranteed by the quota, not by exhaustion.**

---

## 6. Invariants (no change may break these)
1. Bench is serial. 2. The writer never touches the ruler (**benches included**). 3. Correctness before significance. 4. **Verdict + reasons + numbers** all go verbatim into events.jsonl (both judges leave a record, traceable); the tree only reads and never re-scores. 5. Generality (owner/scope via cargo metadata, no piles of special cases). 6. Both judges (deterministic + semantic review) must pass before anything enters the tree; the review is independent of the writer and can reject but cannot loosen anything (AND, not OR).

---

## 7. Existing parts vs to build

| | Status |
|---|---|
| Whole-workspace enumeration (`_workspace_members`) | ✅ exists |
| weak-oracle (passing unit tests is enough) | ✅ exists |
| Autonomous self-written probes (`aro plan`) | ✅ exists |
| Example library seed (`optimization-examples.md`) + soft prompt guidance | ✅ exists |
| Cheap prescreen + dedup + scoring priority | ✅ built in Phase 1 (`eval.prescreen`/`dedup`) |
| Tiered reviewable tree | 🔶 decision-tree.html exists; needs **sorting / clustering / risk flags** |
| **whole-project scope switch** | ❌ to build |
| **maker-checker self-written bench + auto differential for no coverage** | ❌ to build → design merged into `docs/archive/self-extending-search-design.md` (L4a/L4b) |
| **hard example pruning (similarity matching)** | ❌ to build |
| **explore/exploit quota (direction reachability)** | ❌ to build (the core addition of this proposal) |
| **independent semantic review critic gate (bench/code/ideas, adversarial, onto the tree)** | ✅ built (`aro/critic.py`, `--critic` wired into `run_backtest`, verdicts shown on the tree) |

---

## 8. Rollout phases
- **Cross-cutting (spans A/B/C)**: the **independent semantic review critic gate**: wire it in before scoring from the start (code + ideas), and review benches as soon as one is written. It is the second of the "two judges"; the earlier the better.
- **Phase A**: critic gate (code + ideas) + whole-project scope + explore/exploit quota (direction reachability) + tree tiering. ← Gets "wide net, reachable directions, every candidate passes semantic review" running first.
- **Phase B**: maker-checker self-written benches + bench review + auto differential for no coverage. ← Makes "writing its own benches" trustworthy.
- **Phase C**: hard example pruning + the human "add an example" loop at the leaves. ← Makes "reducing the burden" actually take effect.

---

## 9. For your confirmation
1. Is the A→B→C order OK? (Critic gate lands first as the cross-cutting piece; then exploration breadth + direction reachability; then self-written bench credibility; then example pruning.)
2. Are the explore/exploit coverage axes = crate × lens (× subsystem workload) enough? Any other axes to add?
3. Is the default top-of-tree ordering "big wins first + risk flags" enough?
4. **Critic gate**: (1) ✅ **single reviewer** first, interface leaves room for N votes (decided). (2) The review sits **before scoring** (as a smart prescreen that saves throughput): agree? (3) The review is **skeptical by default and rejects when unsure**: is "better to over-reject; a human can overturn at the leaf / add a counterexample" acceptable?
