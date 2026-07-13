# Finding the high-leverage change (the generation lens + adoption)

The judge decides whether a change is real; this doc is about the GENERATOR: how to
produce a candidate worth judging. A profiler tells you WHERE the time goes; it does not
tell you WHICH change to make. Left unguided, an agent reliably finds the right hot
function but then defaults to the lowest-leverage safe change (an `#[inline]`, a CSE) and
stops. The two layers below are what move it from "make this faster" to "is this work even
necessary?": the question that finds algorithmic wins.

## Layer 1, the lens (generation): work the tiers in order

For the measured hot function, in order, highest leverage first:

1. **ELIMINATE (highest value).** Which operations are UNNECESSARY: work whose result
   this path cannot change, is already determined, or is guaranteed by an invariant the
   surrounding code maintains? For each expensive sub-operation: *"does this path genuinely
   need this, or is it redundant given what actually changes here?"* Canonical instances: a
   broad N-way check when only one part of the state could have changed on this path;
   re-validating a condition an upstream caller already ensured; recomputing a loop-invariant.
   **Deleting redundant work beats making it faster.**
2. **WEAKEN.** If the work is genuinely necessary, replace it with a cheaper operation that
   yields the IDENTICAL result: strength reduction, a leaner data structure, caching a
   repeated computation.
3. **CODEGEN (lowest value).** Inlining, removing a copy/allocation. These rarely clear the
   noise floor on their own: do not stop here until tiers 1 and 2 are ruled out.

Enumerate 2-4 candidates across the tiers, rank by **leverage × provability**, pick the
highest-leverage one you can prove byte-identical.

## Pre-proposal checklist (Ir gate)

Before proposing any candidate, answer both:

1. **Would LLVM already do this under release codegen (thin LTO, CGU 16)?**
   Dedup / hoist / strength-reduction default to YES — state why not, or don't propose it.
   (Case law: mega-evm #326 sload-hoist, #332 saturating_sub→plain-sub — both 306/306
   CodSpeed untouched; the compiler already performed the rewrite.)
2. **State the expected Ir movement**: which probe / which bench rows, and rough magnitude.
   Claims are adjudicated by instruction counts, not wall-clock. Semantically-inert relinks
   can show consistent wall-clock deltas up to ~8.4% (mega-evm #335 layout-noise floor).

## Layer 2, adoption: resolve the invariant, don't retreat

The lens makes the agent GENERATE the structural candidate; it then tends to REJECT it
because its safety depends on a non-local invariant, and retreat to a trivially-safe small
change. That retreat is the failure mode to kill.

> If an ELIMINATE candidate is safe ONLY IF some invariant holds ("narrowing/removing this
> is safe because X always holds here"), that is usually the candidate most worth pursuing.
> **RESOLVE the invariant instead of discarding it:** trace every site that could violate it
> (search the crate for all mutators of the state involved), confirm each already self-guards
> (validates / records / latches / checks its own effect) before control returns to the hot
> path, then **COMMIT**: state the invariant and add an **in-code `debug_assert!`** that pins
> it (the candidate patch may NOT edit `tests/`: the guard rejects it; behaviour coverage is
> the adversarial differential's job, see `harness-protocol.md`), and rely on that differential
> to confirm byte-identical behaviour. A
> high-leverage change you have PROVEN beats a trivial change that is merely obviously-safe.
> Fall back to a smaller change ONLY if, after genuinely investigating, the invariant fails.

The judge (adversarial differential + A/B + CI) is the safety net that makes this sound:
the more trustworthy the measurement, the bolder the generator should be. So the
differential must be **adversarial**: it must exercise exactly the paths the safety
argument depends on (workloads that push the state you claim "can't change here" toward /
over its limit), not just the happy path.

## Reflect escalation (across rounds)

Iteration alone does not reach the structural change: it digs deeper in the same direction.
So when a round's change was a local/codegen tweak judged within-noise, the next round must
NOT repeat that tier on the same site; it must escalate to ELIMINATE and name the invariant
to investigate. (Encoded in `skill/prompts/reflect.md`.)

## Evidence (the A/B that validated this)

On a blind benchmark (the target reverted to before a known optimization, the answer made
unreachable in git), three arms with single-variable prompt differences:

| arm | prompt | behaviour | judge |
|---|---|---|---|
| A (×2 rounds) | baseline | dug deeper locally (inline → CSE); never considered the structural change | within-noise |
| B | + lens | GENERATED the structural change as top candidate, but RETREATED at its safety invariant | within-noise (fallback) |
| C | + lens + adoption | generated it, RESOLVED the invariant (traced every mutator, confirmed each self-latches), committed with `debug_assert!` + adversarial differential | **accepted, −72% (CI excludes 0)** |

Arm C independently reproduced the optimization the benchmark was built from. Conclusion:
**both layers are necessary**: the lens makes the win reachable (A never got there); the
adoption layer makes it land (B generated but retreated).
