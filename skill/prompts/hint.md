Profiler-measured hot path (self-time, in-binary compute frames): $top

Optimize the MEASURED hot function above. Propose exactly ONE behaviour-preserving change: byte-identical output for every input (a differential probe checks this). Work the tiers IN ORDER — highest leverage first:
1. **ELIMINATE** redundant work: is the hot path doing work whose result is already determined, or that an invariant makes unnecessary? (e.g. a broad N-way check when only one part of the state can change here; re-validating something an upstream caller already guaranteed; recomputing a loop-invariant.) Deleting redundant work beats speeding it up. If its safety depends on an invariant, do NOT retreat to a smaller change — RESOLVE the invariant (trace every mutator of the state, confirm each self-guards) and pin it with a `debug_assert!`/test; the adversarial differential is your safety net.
2. **WEAKEN**: replace an expensive operation with a cheaper exactly-equal one — strength reduction (e.g. a multiply by a small constant into a few additions where ring/field laws make it identical), a leaner data structure, caching a repeated computation.
3. **CODEGEN (last resort):** inlining, cutting a copy or an avoidable heap allocation — these rarely clear the noise floor on their own.
Do NOT change the result, the public API, the tests, or the benchmark. Cite the exact lines/values you change and why it is byte-identical.
$code
