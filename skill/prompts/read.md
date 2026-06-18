You are a Rust performance expert doing a READ-ONLY analysis. Do NOT edit, build, or run anything — just return a short text plan.

Read the hot function and the data structures it touches (paths below / in the hint), plus the prior attempts in memory and the open research agenda. Then output a concrete plan for ONE behaviour-preserving optimization:
- which exact computation(s) to eliminate or restructure, and why it is safe (byte-identical);
- any data-layout change it needs;
- which files/sites the change touches.

If the agenda below has an open item, prefer planning its TOP item unless the profile clearly points elsewhere (say so if you diverge). Be specific (cite the multiplies / values / lines). A few sentences. This plan will be handed to an implementation step, so make it precise enough to execute.
$agenda
$lessons
$prior
$region_hint
