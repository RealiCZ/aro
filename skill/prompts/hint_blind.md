Profiler-measured hot path (self-time, in-binary compute frames): $top

Optimize the MEASURED hot function above — not a readable-but-cold path. Propose exactly ONE behaviour-preserving change: the output must stay byte-identical for every input (a differential probe will check this). Look for redundant or loop-invariant computation to eliminate, an avoidable allocation on the hot path to cut, or an expensive operation to replace with a cheaper exactly-equal one. Do NOT change the algorithm's result, the public API, the tests, or the benchmark. No technique is prescribed — derive the change yourself from the profile and the code below, and state precisely why it is safe.
$code
