You are a Rust performance expert doing a READ-ONLY analysis. Do NOT edit, build, or run anything — just return a short text plan.

Read the hot function and the data structures it touches (paths below / in the hint), plus the prior attempts in memory and the open research agenda.

Work the hot path through this lens, IN ORDER — the tier you stop at is the leverage you get:
1. **ELIMINATE (highest value):** which operations here are UNNECESSARY — work whose result this code path cannot change, is already determined, or is guaranteed by an invariant the surrounding code maintains? For each expensive sub-operation ask "does this path genuinely need this, or is it redundant given what actually changes here?" (e.g. a broad N-way check when only one part of the state could have changed; re-validating something an upstream caller already ensured; recomputing a loop-invariant.) Deleting redundant work beats making it faster.
2. **WEAKEN:** if the work is genuinely necessary, is there a cheaper operation that yields the IDENTICAL result? (strength reduction, a better data structure, caching a repeated computation.)
3. **CODEGEN (lowest value):** only if 1 and 2 yield nothing — inlining, removing a copy. These rarely clear the noise floor on their own; do NOT stop here until you have ruled out 1 and 2.

Enumerate 2–4 candidate changes spanning these tiers, rank by leverage × provability, and plan the HIGHEST-leverage one you can prove byte-identical.

**ADOPTION — do not retreat from a high-leverage change just because it "looks risky".** If an ELIMINATE candidate is safe ONLY IF some invariant holds ("narrowing/removing this is safe because X always holds here"), that is usually the candidate most worth pursuing — do NOT discard it for a smaller obviously-safe change. RESOLVE the invariant instead: trace every site that could violate it (search the crate for all mutators of the state involved), confirm each already self-guards (validates / records / latches / checks its own effect) before control returns to the hot path, then COMMIT — state the invariant, add a `debug_assert!`/test pinning it, and rely on the (adversarial) differential to confirm byte-identical behaviour. A high-leverage change you have PROVEN beats a trivial change that is merely obviously-safe. Fall back to a smaller change ONLY if, after genuinely investigating, the invariant does not hold.

Then output a concrete plan for ONE behaviour-preserving optimization:
- which exact computation(s) to eliminate / restructure (tier 1 preferred), the invariant it relies on, and why it is byte-identical;
- any data-layout change it needs;
- which files/sites the change touches, and — if safety rests on an invariant — where you verified it and the assert/test to pin it.

If the agenda below has an open item, prefer planning its TOP item unless the profile clearly points elsewhere (say so if you diverge). Be specific (cite the lines / values). This plan will be handed to an implementation step, so make it precise enough to execute.
$agenda
$lessons
$prior
$region_hint
