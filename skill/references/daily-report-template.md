<!--
ARO daily optimization report: fill-in template. Placeholders {{...}} are filled by
daily-report-protocol.md from one explore round's artifacts (events.jsonl / a{N}/patches /
a{N}/records.jsonl / trajectory.svg). Every number is copied VERBATIM from events.jsonl,
never re-judged. Structure follows the PR#313 optimization write-up:
TL;DR callout → 1. what changed (with code) → 2. how much it improved → 3. what to do next.
The four required answers: what changed / how much it improved / what code changed / what to do next.
-->

# ARO daily optimization report · {{target}} · {{date}}

**This round's workload**: {{workload_description}} _(probe file `{{workload_probe}}`)_

> 🕵️ **TL;DR**
> - **Goal**: on the workload above, automatically find byte-identical optimizations and prove with a deterministic judge that they are not noise.
> - **What was done**: automatically attempted {{n_attempts}} hot functions → **{{n_accepts}} landed** ({{accept_one_liner}}), {{n_within_noise}} stayed within noise and did not pass.
> - **Result**: the workload as a whole is **{{realized}}% faster**, judge-proven (A/A floor + paired A/B + differential). {{relaxed_note}}
> - **Decision**: **{{decision}}**: {{decision_reason}}

## 1. What changed (with code)

**{{n_accepts}} landed** (judged accepted):

| What changed | Why it was waste | File | Δ |
|---|---|---|---|
{{#each accept}}| {{what}} | {{why_waste}} | `{{file}}` | **{{delta}}%** |
{{/each}}

{{#each accept}}
> **Code ({{fn}})**: {{code_summary}}. Full patch: `{{patch_path}}`.
{{/each}}

**Tried but did not pass** (recorded honestly, like the ablation that logged "Vec to field: zero change" to close the direction; do not report only the successful optimizations):

| Function | Δ | Conclusion |
|---|---|---|
{{#each within_noise}}| `{{fn}}` | {{delta}}% | {{note}} |
{{/each}}

**Skipped (unreachable)**: {{skipped_fns}}: macro-generated / inlined, no `fn` to locate ({{unreachable}}% of the profile, see D3 below).

## 2. How much performance improved

![trajectory](trajectory.png)

_Chart: realized (solid blue line, rising, % faster already landed) vs addressable headroom (dashed orange line, falling, what is left to optimize); hollow orange dots = relaxed-regime optimizations (need a human decision); the box at the end = decision {{decision}}._
<!-- Must be a PNG, not .svg: markdown previewers mostly do not render SVG. First run `qlmanage -t -s 1100 -o DIR DIR/trajectory.svg && mv DIR/trajectory.svg.png DIR/trajectory.png`. -->


| Quantity | Value | Meaning |
|---|---|---|
| **Realized** | {{realized}}% faster | compounded cumulative, bench-measured, {{n_accepts}} accepts |
| **Addressable** | {{addressable}}% | our own functions, still locatable and untried (Amdahl upper bound) |
| **Unreachable** | {{unreachable}}% | macro-generated / inlined, cannot be named yet |
| **Floor (hands-off)** | ≈{{floor}}% | not-ours ({{floor_owners}}) |

> Measurement: A/A noise-floor calibration → paired A/B (randomized order) → bootstrap CI excludes 0 → random-input differential proves byte-identical. Every Δ is copied VERBATIM from events.jsonl, never re-judged.

## 3. What to do next

**Deterministically synthesized** from this round's end-state (not guessed); pick one as the next round:

| Direction | Unlocks | Cost | Who decides |
|---|---|---|---|
{{#each direction}}| **{{id}}** {{title}} | {{unlocks}} | {{cost}} | {{owner}} |
{{/each}}

> Honest note: same workload, same regime, a run tomorrow will most likely STOP at the start (everything addressable this round is already marked tried).
> What makes the next round productive is the regime decision (switch workload / relax the rules / upgrade the optimization technique); the judge does not make it for you.

**Decisions you need to make now**
{{#each decision_needed}}{{n}}. {{text}}
{{/each}}

---

> **Terms**: **regime** = the rule set this optimization was found under. **byte-identical** = behaviour fully unchanged, can be merged directly; **relaxed** = changed structure, should not be merged directly, needs a human decision (should-not-merge). **optimization / proven optimization** = the candidate was scored by the deterministic judge and confirmed a real speedup.
