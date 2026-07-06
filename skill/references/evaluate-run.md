# Evaluate a run as an independent reviewer

For the accepts the system itself refused to auto-PR (`mergeable:false`): form your OWN
judgment about each edit and decide, per edit, whether to open a PR against the target
repo, reject it, or escalate to a human. Rejecting everything is an acceptable outcome.
This protocol is the judgment counterpart to `run-to-pr.md` (which handles the mechanical
`mergeable:true` path and stops at "route the rest to a human"; that human can be you,
acting under this protocol, when the maintainer has delegated the review).

## Where the data is (per run directory)

| Artifact | What it is |
|---|---|
| `manifest.json` | the final accepted edit set: per edit `fn`, `files`, measured `delta_pct`, `regime`, `critic_verdict`, `hypothesis` (the optimizer's claim, verbatim), `patch_path` |
| `a<N>/patches/*.txt` | the edits in SEARCH/REPLACE form (format: `run-data.md`) |
| `events.jsonl` | the verbatim run log: every gate, bench, verdict, and the critic's full per-candidate audit reasons |
| `decision-tree.html` | the same data rendered; click a candidate row for its dossier |

Field semantics and provenance rules: `run-data.md`.

## What the flags mean (contract, not opinion)

- `accepted` = the deterministic judge proved correctness-preservation (build + tests +
  byte-identical random-input differential) and a statistically significant speedup on
  the campaign workload. It says NOTHING about architecture, readability, or whether the
  win is worth the change.
- `regime: relaxed` = the function carries a historical reviewer objection in the
  optimizer's memory. `critic_verdict: pass-risk` = the second judge passed it while
  flagging concerns; its reasons are in `events.jsonl`. `mergeable: false` = the system
  refused to auto-PR; a human-grade judgment call is required.

## The review, step by step

1. **Read the actual diff**, the hypothesis, and the critic's reasons for each accepted
   edit. Never judge from the hypothesis alone.
2. **Judge as the target repo's maintainer would**: is the correctness argument sound in
   ALL builds (what a `debug_assert` guarantees in release: nothing); what is the tail
   risk if a pinned invariant drifts later, and is the failure mode loud or silent; does
   the edit dissolve a layer or respect it; is the measured win worth the change at all.
3. **Mind composition**: later edits to the same file build on earlier ones (each SEARCH
   anchors to the previous state). Decide how they compose and how to group them into
   PRs; a later edit may subsume an earlier one, in which case present the SQUASHED
   final state, not the archaeology.
4. **Verify empirically on the target's CURRENT main** before trusting any number: apply
   the edit(s) in manifest `order`, run the spec's build and test commands, and re-run
   the byte-identical differential (the spec's diff probe works as a cargo example in a
   scratch tree; remove it before committing). A SEARCH block that no longer applies
   cleanly is INFORMATION (the region changed since the baseline), not an obstacle to
   force through.
5. **Write the verdict down**, per edit: merge-worthy (open a PR), rejected (why), or
   escalate (what a human must weigh). Verdicts for ALL edits come before the first
   side effect.
6. **Then act, under `pr-discipline.md` in full**: decide-first grouping (a change
   appears in exactly one PR; squashed final states, not archaeology), both test-evidence
   gates, number provenance, body content, and the violation-grade process rails.

## Hard rails (this path's own, beyond `pr-discipline.md` section 5)

- Finish with a written report: per edit, verdict and reasoning, plus links to any PRs
  opened. The report is the deliverable even when nothing gets a PR.
- Escalation is a first-class outcome: when the judgment genuinely belongs to a human
  (risk/benefit at the edge), say so in the report instead of forcing a verdict.
