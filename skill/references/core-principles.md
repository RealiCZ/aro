# Core principles

The "why" behind the rules in SKILL.md. ARO's whole bet: in a world where a strong model + a thin loop can write almost any optimization, the scarce thing is *believing the result*. These principles protect that.

1. **The loop is commodity; the judge is the moat.** Driving a model to propose changes is a one-line shell loop. What's hard — and what ARO invests in — is a verdict you can trust on a sub-1% change buried in noise.

2. **The writer cannot grade itself.** The agent that wrote the code is the worst judge of it. Scoring is done by a separate, deterministic evaluator (maker-checker, like a bank). Never let a prompt "decide" if a change is better.

3. **Scoring is executed code, never model reasoning.** Statistics reasoned by an LLM are non-reproducible and gameable. The noise floor, the paired A/B, the bootstrap CI, and the guard are real code that runs the same way every time. That ~250-line core is the only part that *must* be code.

4. **Measurement noise is the adversary.** Same code, same machine, two runs differ. If a Δ is smaller than that wobble, "faster" is luck. So: calibrate the wobble (A/A floor), and require a paired bootstrap CI that excludes 0. A real gain clears both, or it doesn't count.

5. **Behaviour-preserving is sacred.** For consensus / crypto / EVM code, "faster but wrong" is a disaster. Correctness (build + test + differential vs the frozen baseline) is a hard gate *before* any speed measurement. This is the gate `autoresearch` has no need for — and exactly why it can't be used here directly.

5b. **Byte-identical + faster is necessary, NOT sufficient — maintainability is a real cost the judge can't see.** The judge proves correctness and speed; it cannot measure whether a change keeps the code readable and changeable, and a human reviewer *will* reject a faster change that breaks a layer, makes one case the sole exception to a documented convention, conflates two responsibilities, or hurts discoverability. So the generation prompts carry the maintainability filter and a worked example catalogue (`optimization-examples.md`, anchored on the real PR #313 SSTORE-inlining rejection). The agent must prefer the layer-preserving variant — thread the loaded value through the existing interface rather than dissolving it — and, if it still trades structure for speed, FLAG it so it surfaces as should-not-merge (a `relaxed` win), never a clean accept. *This is exactly why the team did not take the double-`inspect_storage` win by hand.*

6. **Profile the real hot path; don't optimize readable code.** A classic mistake is tuning a readable-but-cold path (~1% of the time) while the real hot kernel (~76%) sits untouched. The observe arm (profiler) is load-bearing: it tells the generator where the time *measurably* is.

7. **Read before write.** Derive a precise plan from the code read-only, then implement it. Deriving the insight and executing the multi-site change are different problems; conflating them wastes the expensive write loop.

8. **Memory compounds; one round proves nothing.** Hit rate per round is low by design — most candidates are sub-noise. The value is multi-round: accepted patches fold into the baseline, dead ends steer the next prompt. Judge it over days, not a single round.

9. **Generality is via spec, not code.** A new target is a declarative spec file, not a new Python class. The loop, judge, and generator never change per target.

10. **Stop on the goal, not the clock.** The system has an explicit objective and stop condition. It ends when the target is met or returns dry — not after an arbitrary N rounds, and not by killing the agent mid-work.
