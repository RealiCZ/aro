# ARO Self-Extending Search: Infinite Attempts, Self-Grown Benches and Test Cases, Exhaustive Decision Tree (design doc v1, pending review)

> **Historical design document** — may not reflect the current system. See [OPERATIONS.md](../OPERATIONS.md) and [ONBOARDING.md](../ONBOARDING.md) for what ships today.


*Status: executed. L4a/L4b/L4c landed on branch refactor-2026-07; kept as the decision record.*

> Goal restated (your end requirement): **given a Rust project, the system tries indefinitely, grows its own benches and
> test cases, walks every branch, records key information and commentary at every node, and after exhausting the space
> squeezes out the last provable bit of performance.**
>
> This doc answers two questions: (1) what to change so the system can "grow its own benches/test cases" without corrupting the judge;
> (2) language choice. Conclusion: **add three capability layers (L4a probe factory / L4b workload factory / L4c permanent decision tree),
> with judge semantics untouched down to the line; the language stays Python.**

---

## 0. Where today falls short of the goal

Today's search space is finite, capped by three things:

| Capping factor | Today | Consequence |
|---|---|---|
| **Single workload** | one spec = one `benchmark_probe`; the frontier = that workload's hot functions | branches that workload never executes never enter the frontier; the infinite-flow design §4.7 itself admits "automatic multi-workload is the biggest missing piece" |
| **Fixed measurement power** | the full-workload bench noise floor is ~0.5-2%; a real win below the floor can only be judged `noise-limited` | a batch of nodes where "the CI excludes 0 but cannot beat the floor" hangs forever; the space cannot be squeezed dry |
| **One-shot probes** | probes are written once per target by `aro plan` and never grow afterwards | new functions/branches have no matching bench and differential to measure them |

The mechanical part of "infinite attempts" (exhaustive frontier, multi-round reflect, fan-out, prescreen) already landed in phase 1.
So what is missing is not "more loops"; it is **self-production of probes and workloads, plus a permanent cross-run tree**.

---

## 1. Language choice (conclusion first: keep Python)

**The deciding question is where the bottleneck is.** ARO is an orchestrator: wall clock is dominated by Rust compilation (minutes),
serial benches (minutes), and `claude` generation (minutes); the CPU time of the Python code itself is negligible.
Switching languages buys no throughput at all. Item by item:

- **Rust rewrite**: type safety is a real gain, but 90% of this code is "assemble prompts, spawn subprocesses, parse
  JSON/text": Rust iterates several times slower on that kind of work, and ARO is itself a codebase that agents modify at high
  frequency (self-extending search means agents edit the orchestration logic on the spot). A compile step would slow the whole
  self-development loop. The benefit is available far cheaper: gradual mypy checking (already scheduled in refactor plan P1/P3).
- **JS/TS**: no advantage over Python, a weaker subprocess/statistics ecosystem, and it adds a node runtime dependency,
  breaking the "pure stdlib, zero dependency" distribution promise. JS is already where it belongs: the viz report frontend.
- **Unique reasons Python stays**: `concurrent.futures` is more than enough for subprocess concurrency (the GIL is
  released while waiting on subprocesses); small-scale statistics like the bootstrap CI run in microseconds in pure Python; zero-dependency
  single-directory distribution matters for "drop it on any machine and it runs".

**Conclusion: keep the current boundary. Python orchestrates, Rust is the language of the target and the probes, JS/Svelte does only the
report UI.** If some statistic ever becomes genuinely too slow (say, million-scale resampling), add an optional numpy path;
a rewrite is not worth it.

---

## 2. Core principle: self-extension must never corrupt the judge

Writing your own benches and your own test cases collides head-on with this project's founding rule: "**writers never judge themselves**"
(Gate 0 explicitly forbids patches from touching `benches/` and `tests/`). Letting the system produce its own judging tools without
fooling itself rests on three iron rules:

1. **Role separation**: the patch-writing agent and the probe-writing agent are two independent invocations that never see each other's output.
   The patch generator's Gate 0 constraint is unchanged: probe files are never inside its editable area.
2. **Time separation (probes freeze first)**: a node's probe must be finalized **before** any candidate for that node is generated;
   its content hash goes into events (new `probe_registered` event: fn, path, sha256, qualification-gate
   results). Every candidate at that node is then judged with the probe pinned by that hash: a probe can never be written
   after the fact to flatter some patch.
3. **Probes must pass a "probe judge" qualification gate** (details in §3.1/§3.2): an unqualified probe is discarded and rewritten in full,
   never put to work sick.

> This is isomorphic to the existing architecture: the judge judges patches, the probe judge judges probes. **Judging power always stays
> in deterministic code; agents only produce.**

---

## 3. The three new capability layers

```
           ┌───────────────── L4b workload factory ─────────────────┐
           │ agent proposes a new workload → qualification          │
           │ gate (determinism + coverage increment + oracle        │
           │ mutation test) → registered into workload set W        │
           └───────────────────────────┬────────────────────────────┘
                                       ▼
   for each w in W:  profile → frontier (existing L3 loop;
                     phase 1 already made it parallel/exhaustive)
                                       │
        node noise-limited or diluted? ──────► L4a probe factory:
                                       │          agent writes an isolated micro bench
                                       │          → qualification gate (A/A floor +
                                       │          relevance + freeze first) → re-judge
                                       │          with the new probe
                                       ▼
           ┌───────────── L4c permanent decision tree ──────────────┐
           │ stable node ids, accumulated across runs and           │
           │ workloads; per node: verdict/Δ/CI/floor/               │
           │ probe hash/hypothesis/critic notes/reflect             │
           │ directions; exhaustion proof = all nodes closed        │
           └────────────────────────────────────────────────────────┘
```

### 3.1 L4a: isolated probe factory (grow your own benches: self-extending measurement power) ★ do first

**Solves**: the `noise-limited` cold cases and the small hot functions diluted by the full workload; "squeezing out the last bit" is stuck mainly here.

- **Trigger**: a node is judged `noise-limited` (CI excludes 0 but cannot beat the floor), or the function's self-time
  share is too small (below the floor's resolvable threshold), and auto-tighten raising scale cannot save it.
- **Production**: reuse the existing machinery of `aro plan` (`plan._fill_slots` already dispatches an agent to write probes in a
  throwaway worktree, plus a dry-run check), reshaped into `probe_factory.author(fn, files)`:
  for a single function, write a cargo example micro bench that hugs it (construct a realistic input distribution, call the
  function in a loop, output a `BENCH <metric>=<val>` line, respect `ARO_BENCH_SCALE`).
- **Qualification gate (the probe judge, deterministic code)**:
  1. **A/A qualification**: run A/A calibration on the new probe; its floor must be significantly lower than the parent workload's floor (otherwise switching is pointless);
  2. **Relevance**: profile the micro bench itself; the target function's self-time share must be ≥ 60% (it really measures that function);
  3. **Scale awareness**: doubling `ARO_BENCH_SCALE` should roughly double the time (so auto-tighten stays effective);
  4. **Freeze first**: hash into events, and only then may candidate generation start for that node.
- **The correctness oracle does not change**: the micro bench takes over **Gate 2 (measurement)** only; **Gate 1 still uses the parent workload's
  differential + test suite**: a hot function is necessarily executed by the parent workload, so its behavior is already constrained by the
  full-workload byte-identical check. This is v1's most important safety design: self-produced probes only affect
  "can we resolve it", never "is it correct".
- **Acceptance rule (against "optimizing for the synthetic bench")**: for a micro-bench win to fold into the baseline, it must also pass a
  **parent-workload re-check**: paired A/B on the parent workload must at least show no significant regression. The node records two levels of evidence:
  micro-bench Δ/CI (proof the win exists) + the parent-workload effect (the Amdahl-converted overall contribution). A win provable
  only on the micro bench, unresolvable on the parent workload, gets the new regime label `micro-proven` (mergeable rules stay conservative).
- **Touch points**: extract the probe-generation machinery from `plan.py` into `aro/probe_factory.py`; inside `attempt()`,
  `dataclasses.replace(spec, ...)` already supports swapping the spec per node: additionally replace the `bench` slot;
  guard needs no change (probes are outside the patch's editable area).

### 3.2 L4b: workload factory (grow your own test cases: self-extending coverage)

**Solves**: the single-workload blind spot: the branches in "walk every branch" that the current workload simply never executes.

- **Production**: an agent reads the repo (bench example, tests, public API) and proposes a new deterministic workload
  variant (different input distribution / different operation mix / pressure on different code paths), together with a matching differential
  probe (deterministic pseudo-random input → fingerprint output). The product = one new spec entry
  (`targets/<name>/workloads/<w>.json` + `probes/<w>.rs` + `probes/<w>_diff.rs`).
- **Qualification gate**:
  1. **Determinism**: two runs with the same seed produce identical fingerprints;
  2. **Coverage increment**: profile the new workload; its frontier must contain ≥1 of the project's own functions that is not hot
     under the existing workload set (decided against the known node set in the L4c tree): a workload that adds no new frontier quality is rejected outright;
  3. **Oracle mutation test** (the qualification certificate for a self-produced differential): seed k known
     behavior mutations into the hot path (flipped comparison operators, off-by-one, etc., inside a throwaway worktree); the new differential must
     flag every one. **An oracle that cannot catch seeded mutations has no authority to certify byte-identical**: this is the key gate that keeps
     "writing your own test cases" honest;
  4. Freeze first + hash into events (same as §2).
- **Honesty boundary (the conflict with the existing principles, and its resolution)**: the infinite-flow design §2.3 lists "changing the workload"
  as one of the two genuinely human gates (representativeness is a domain judgment). Your new requirement is full automation. The resolution: **automate
  the mechanically decidable part, and turn the domain judgment into a label instead of a gate**: the workload factory produces automatically, the
  qualification gate admits automatically, the search runs automatically, but every win in the tree carries its **workload provenance**;
  `mergeable` still only recognizes wins that hold on the human-approved original workload; wins on new workloads are labeled
  `synthetic-workload` and listed at PR time under "needs human confirmation of representativeness".
  Unattended operation is not reduced; this is honesty tiering.
- **Touch points**: the spec upgrades to a workload set (`workloads: []` or a directory convention); a per-workload
  scheduling loop wraps around `attempt()`; lessons and the tree account per (workload, function).

### 3.3 L4c: permanent decision tree (the exhaustive ledger across runs and workloads)

**Solves**: "walk every branch + key information and commentary per node". Today the tree is **single-run**
(derived from that run's events.jsonl), and tried-state relies on text matching against lessons.jsonl (fragile).

> **The tree grows dynamically; it is not defined up front (static rules, emergent instances).** At startup there is only
> the repo plus workloads; the first profile produces the first layer of nodes. After that, five mechanisms make it grow:
> (1) re-profiling after an accept surfaces below-threshold functions as new nodes; (2) the lens ladder
> expands depth on demand by dry-round count; (3) reflect proposes new d1/d2/d3 branches on the spot; (4) noise-limited
> nodes trigger L4a and grow a "re-judge on the micro bench" subtree; (5) a new workload brings a whole new top-level subtree.
> Fixed in advance are only the node dimensions (workload × function × lens × baseline hash), the judge rules, and the growth gates
> (min_pct, coverage increment, attempt caps, probe qualification): isomorphic to MCTS/branch-and-bound: expand on
> demand, prune by gates, terminate by exhaustion proof, so unbounded growth still converges to closure.

- **Node identity**: stable key = `(workload id, function symbol+file, lens layer, baseline patch-set hash)`:
  a revisit attaches to the same node automatically; after the baseline advances, the same function is a new node (because the object under study changed).
- **Per-node record** (the "key information + commentary" you asked for; almost all of it is already in events, only per-node aggregation is missing):
  - Key information: verdict, Δ%/CI/floor/scale, regime (byte-identical / micro-proven /
    synthetic-workload / relaxed), probe id+hash, time/token cost;
  - Commentary: the candidate hypothesis, the read-phase plan summary, the critic's itemized reasons, the follow-up
    directions produced by reflect (d1/d2/d3) and their resolution status.
- **Storage**: `store.py` grows a `tree.jsonl` (append-only, same discipline as events: reports read,
  never rewrite); `decision-tree.html` upgrades to a multi-workload view (workload → function → lens → candidate, four levels).
- **The honest definition of exhaustion (when "squeezed dry" may be claimed)**: the tree delivers a **proof** of three boundaries, not a feeling:
  1. **Untouchable floor**: the crypto/runtime share (the Amdahl asymptote);
  2. **Measurement floor**: every noise-limited node is either settled by an L4a micro bench, or records
     "probe power is maxed out and it still cannot be resolved" (the floor no longer drops);
  3. **Coverage closure**: the workload factory's proposals fail the "coverage increment" gate N times in a row.
  All three closed → every node in the tree has a final verdict → **that is the machine-checkable proof that "exhaustion is complete".**

---

## 4. Landing order (relation to the refactor plan, docs/archive/refactor-plan.md)

The self-extending capabilities **depend** on these pieces of the refactor plan; the order cannot be flipped:

| Dependency | Why |
|---|---|
| P1 fixture E2E | the probe factory and the workload factory both touch the real paths in `plan.py`/`target.py`; do not touch them without a net |
| P2 runlog unification | the L4c permanent tree must read events across runs; merge the 4 mutually contradictory readers into 1 first |
| P2 llm.py / vcs.py | the probe factory is just "a few more claude calls + worktrees"; consolidate first, then reuse |
| P4 profiler temp-dir fix (C5) | parallel multi-workload profiling would overwrite `/tmp/aro_sample.txt` |
| P3 attempt split | L4b's workload scheduling layer wraps around attempt; it cannot wrap around the 1049-line sweep |

**Recommended execution order**:

```
P0+P1 (hygiene + safety net) → P2 (dedup) → L4a probe factory ← earliest new-capability payoff
→ P3 (structural split, includes C5) → L4c permanent tree → L4b workload factory → P4/P5 wrap-up interleaved
```

Size: L4a ≈ 4-5 days (plan machinery rework + qualification gate + attempt wiring + fixture cases);
L4c ≈ 3-4 days (store extension + multi-workload tree UI); L4b ≈ 1.5-2 weeks (workload proposal prompt,
mutation-test framework, multi-spec scheduling). All agent-driven; actual wall clock is shorter.

---

## 5. Risks and the most fragile assumption

- **Most fragile assumption: "a hot function is necessarily covered by the parent workload's differential" (the correctness foundation of L4a).**
  If a function is hot in the bench yet barely affects the differential fingerprint (say, a pure statistics/logging path), the byte-identical
  constraint is weak for it. Mitigation: add a 5th check to the L4a qualification gate: seed 1 mutation into the target function; the parent
  workload's differential must flag it. Functions where it cannot are automatically required to get an L4b-grade dedicated differential, or are labeled
  `weak-oracle-node` and handled as downgraded.
- **Synthetic-workload representativeness risk** (L4b): the machine can only prove "deterministic + adds coverage + oracle-sensitive";
  it cannot prove "looks like production traffic". Already contained by provenance labels + the conservative mergeable rules (§3.2).
- **Probe factory output running wild**: building a probe for every noise-limited node would blow up the serial bench queue.
  Control: probes are produced only for nodes with self-time ≥ a threshold (default 1.5%, same as min_pct);
  queue priority keeps the smoke-Δ ordering.
- **"The exhaustion never finishes" is not a risk; it is a property the design absorbs (anytime behavior)**:
  (1) termination is guaranteed in theory: function nodes are capped by Amdahl+min_pct, each function has lens-layer and
  dry-round gates, probe power has a "floor no longer drops" ceiling, the workload coverage-increment gate must run dry over a finite
  function set, and headroom decreases monotonically; (2) but finite is not fast (a large repo × many workloads can mean days to weeks of wall clock),
  so the loop is anytime: every accept is banked on the spot (events/manifest/permanent tree), an interruption at any moment
  = a pause rather than a loss, and resume continues from the advanced baseline; (3) the search walks in descending expected value (hottest first +
  smoke-Δ queue ordering), so the big wins come first, the marginal-return curve is visible in every step report (headroom/
  realized/floor), and the two safety valves stay: `--max-attempts` and the automatic STOP at headroom≤2%.
- **Rollback**: all three layers are additive, each behind its own independent flag (`--probe-factory` / `--workloads` /
  the permanent tree defaults on but only appends files); switch them off and you are back to today; zero changes to the judge and the existing event contract.

---

## 6. Open questions (your call)

| # | Question | My recommendation |
|---|---|---|
| W1 | The L4a micro-bench relevance threshold (target function's share of micro-bench self-time ≥ ?%) | Start at 60%, recalibrate after two real targets |
| W2 | The mergeable rules for the two new regimes `micro-proven` / `synthetic-workload` | Neither is auto-mergeable; the manifest gets a dedicated "needs human confirmation" section |
| W3 | The seed mutation set for the oracle mutation test (k=? which mutation classes) | k=3: comparison-operator flip, boundary ±1, early return; certify only if all are caught |
| W4 | Should the L4b workload factory restrict the input domain (vary the input distribution only vs allow new call sequences)? | v1 varies the input distribution only (low risk); call sequences go to v2 |
| W5 | Permanent-tree granularity: store every candidate in full vs node-level aggregation + candidate references into events | Node-level aggregation + pointers back into events (the tree stays light; the truth stays in events) |
