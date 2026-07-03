You are a Rust performance research lead doing a READ-ONLY reflection. Do NOT edit, build, or run anything: return ONLY a JSON object.

This round's candidates and the judge's verdicts:
$results

The current open research agenda (directions already queued):
$agenda
$lessons
$region_hint

Your job: update the agenda for the NEXT round. Mark which open items are now resolved: `dropped` if tried and the verdict was within-noise/regressed, `done` if clearly subsumed by an accepted win: and propose 1-3 NEW high-leverage directions to try next.

Use this priority ladder (autoresearch's Ideate phase), highest first:
  1. fix anything that crashed or failed to build;
  2. exploit a success: if a change improved the metric, propose variants in the same direction;
  3. combine near-misses: two within-noise changes that might compound;
  4. change the data layout / try the opposite of what didn't work: e.g. a within-noise inline-struct layout → try a separate parallel array; if the noise floor was too high to resolve the change, propose raising measurement power (more A/B pairs) rather than a code change;
  5. radical rewrite: only when incremental directions are exhausted.

ESCALATION RULE (important): if this round's change was a local/codegen tweak (inline, copy removal, single hoist) and the verdict was within-noise, do NOT queue another change of the same kind on the same site: the codegen tier is exhausted there. Escalate to the ELIMINATE tier: ask which work on the hot path is REDUNDANT given an invariant the surrounding code maintains (e.g. a broad N-way check when only one part of the state can change here; re-validation of an upstream-guaranteed condition) and whether removing it is provably byte-identical. When you queue such an ELIMINATE direction, name the invariant it depends on so the next round investigates and pins it rather than retreating from it.

Each new direction must be ONE concrete, behaviour-preserving (byte-identical) change on the hot path, with a one-line rationale grounded in THIS round's data (cite the Δ / floor / verdict).

Answer with ONLY this JSON (no prose before or after):
{"resolve": [{"id": "<agenda id>", "status": "done|dropped"}],
 "add": [{"direction": "<one concrete thing to try next>", "rationale": "<why: cite the data>"}]}
