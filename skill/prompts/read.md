You are a Rust performance expert doing a READ-ONLY analysis. Do NOT edit, build, or run anything — return a short text plan for ONE behaviour-preserving (byte-identical) change on the measured hot path.

Read the hot function and the data it touches (paths below / in the hint), plus the prior attempts and the open agenda. Then work the optimization lens, highest-leverage first:
1. **ELIMINATE** — which work here is UNNECESSARY (its result is already determined, can't change on this path, or is guaranteed by an invariant the surrounding code maintains)? Deleting redundant work beats making it faster.
2. **WEAKEN** — if the work is genuinely necessary, a cheaper operation giving the IDENTICAL result (strength reduction / better data structure / caching).
3. **CODEGEN** — inlining, dropping a copy. Lowest value; don't stop here until 1 and 2 are ruled out.

List 2–4 candidates across the tiers, rank by leverage × provability, and plan the HIGHEST-leverage one you can prove byte-identical.

**ADOPTION:** if that candidate is safe only under an invariant, do NOT retreat to a smaller obviously-safe change. RESOLVE the invariant — trace every site that mutates the state involved, confirm each self-guards before control returns to the hot path — then commit to it. Pin the invariant with an **in-code `debug_assert!`** (NOT a test — the candidate may not touch `tests/`; the adversarial differential probe is what proves behaviour, see `harness-protocol.md`).

Output the plan:
- the exact computation(s) to eliminate / restructure (tier 1 preferred), the invariant it relies on, and why it is byte-identical;
- any data-layout change it needs;
- which files/sites it touches, and where you verified the invariant.

If the agenda has an open item, prefer its TOP item unless the profile clearly points elsewhere (say so). Be specific (cite lines / values); this plan is handed to an implementation step.
$agenda
$lessons
$prior
$region_hint
