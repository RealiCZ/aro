You are a Rust performance expert doing a READ-ONLY analysis. Do NOT edit, build, or run anything: return a short text plan for ONE behaviour-preserving (byte-identical) change on the measured hot path.

Read the hot function and the data it touches (paths below / in the hint), plus the prior attempts and the open agenda. Then work the optimization lens, highest-leverage first:
1. **ELIMINATE**: which work here is UNNECESSARY (its result is already determined, can't change on this path, or is guaranteed by an invariant the surrounding code maintains)? Deleting redundant work beats making it faster.
2. **WEAKEN**: if the work is genuinely necessary, a cheaper operation giving the IDENTICAL result (strength reduction / better data structure / caching).
3. **CODEGEN**: inlining, dropping a copy. Lowest value; don't stop here until 1 and 2 are ruled out.

List 2-4 candidates across the tiers, rank by leverage × provability, and plan the HIGHEST-leverage one you can prove byte-identical.

**ADOPTION:** if that candidate is safe only under an invariant, do NOT retreat to a smaller obviously-safe change. RESOLVE the invariant: trace every site that mutates the state involved, confirm each self-guards before control returns to the hot path: then commit to it. Pin the invariant with an **in-code `debug_assert!`** (NOT a test: the candidate may not touch `tests/`; the adversarial differential probe is what proves behaviour, see `harness-protocol.md`).

**MAINTAINABILITY (a reviewer rejects a faster change that worsens this: byte-identical + faster is necessary, NOT sufficient):** before you commit to the plan, run it past this filter:
- **Layer / convention**: does my change make ONE case the sole exception to a uniform pattern the file documents (e.g. "every storage-gas-charging opcode routes through `storage_gas_ext`"), or DELETE a layer that pattern relies on? Did I have to edit a convention table/comment to call out my special case? That's a red flag.
- **Single responsibility / discoverability**: does a function now do a SECOND unrelated job just to reuse a value, or would a reader asking "where does X happen?" now have to discover a special case?
- **Magnitude vs cost**: a few in-memory HashMap probes on a warm path (no I/O saved), with no benchmark isolating the effect, is a SMALL win. Small wins do not justify a structural cost.
If the highest-leverage candidate trips this filter, plan the **layer-preserving variant instead**: thread the already-loaded value DOWN through the existing interface rather than dissolving the boundary (the canonical case: don't inline-and-delete `storage_gas_ext::sstore`; pass the loaded slot into it). If even that isn't possible, pick a different lever. If you still believe a structure-trading change is worth it, plan it but FLAG in the plan which layer/convention it breaks so it surfaces as should-not-merge, not a clean win.

Output the plan:
- the exact computation(s) to eliminate / restructure (tier 1 preferred), the invariant it relies on, and why it is byte-identical;
- any data-layout change it needs;
- which files/sites it touches, and where you verified the invariant.

If the agenda has an open item, prefer its TOP item unless the profile clearly points elsewhere (say so). Be specific (cite lines / values); this plan is handed to an implementation step.
$constraints
$agenda
$lessons
$prior
$region_hint
