Profiler-measured hot path (self-time, in-binary compute frames): $top

Optimize the MEASURED hot function above. Propose exactly ONE behaviour-preserving change: the output must stay byte-identical for every input (a differential probe will check this). Levers worth considering, roughly in order of how provable the win is:
- cut an avoidable heap allocation on the hot path — allocation count is far less noisy than wall-clock, so a real reduction is easy to prove;
- hoist a loop-invariant computation out of the hot loop, or precompute it into a table the loop already reads;
- strength-reduce an expensive operation into cheaper exactly-equal ones (e.g. a multiply by a small constant into a few additions) where the ring/field laws make the result identical.
Do NOT change the result, the public API, the tests, or the benchmark. Cite the exact lines/values you change and why the change is byte-identical.
$code
