# Report protocol (skill-rendered, not coded)

The human run report (`RUN-REPORT.md`) is **rendered by you, the agent, from a run's `events.jsonl`**: there is no `report.py`. The event log is the structured, machine-readable source of truth (the engine writes every number into it); your job is only to narrate it. The discipline below is what keeps a prose report as trustworthy as the judge that produced the numbers.

## Select the run first (run_id)

`events.jsonl` is **append-only**: re-running into the same `--out` keeps every prior run's
events, and each event carries a `run_id`. Before rendering anything: read all lines, find the
**latest `run_id`**, and render ONLY that run's slice. Render an older or different run only when
the user names its `run_id` explicitly. Never mix floors, verdicts, or counts across `run_id`s;
a stale run's noise floor or a prior `regressed` verdict bleeding into a fresh report is exactly
the failure this guards against.

## The one rule that matters

**Copy every number verbatim from `events.jsonl`; never re-compute, re-judge, or soften a verdict.** A report is a *view* of the event log, not a second opinion on it. Specifically:

1. Δ%, CI bounds, floor%, elapsed, counts: copied exactly from the events (round to the events' own precision; don't invent digits).
2. The `verdict` is reproduced as-is. A `within-noise` / `regressed` / `build-failed` candidate is **never** written up as an improvement, a "small win", or "trending faster". The report cannot launder a verdict the judge refused. A `noise-limited` verdict is its own honest category: report it as "a consistent directional effect (CI excludes 0) the measurement could not resolve above its floor even after auto-tightening", NOT as an accepted win; note any `bench_rescaled` events (the scale escalation that was attempted).
3. You do not decide significance. If `improved` is `false`, it did not improve, full stop, regardless of the sign of Δ.
4. Missing field → write `n/a`. Never fill a gap with a guess.
5. A NoOp candidate (empty `files`) is labelled as the control, and its `within-noise` verdict is reported as *evidence the gate manufactures no false positives*: that is the point of it.

If the events disagree with what would make a nicer story, the events win.

## Input → section mapping

Read the run's `events.jsonl` (one JSON object per line; see `results-logging.md` for the vocabulary) and map fields to sections:

| report section | events source |
|---|---|
| title / overview (target, baseline, rounds, candidates, accepted, elapsed) | `run_started` + `run_finished` |
| noise floors | `floors_calibrated.floors` (`{metric: floor%}`) |
| per-candidate hypothesis + files | `candidate_proposed` (`id`, `hypothesis`, `files`) |
| per-candidate Δ% / CI / floor / verdict | `candidate_verdict` (`verdict`, `deltas[]` = `{metric, delta_pct, ci_low_pct, ci_high_pct, floor_pct, improved}`) |
| gate trace (what passed / where it stopped) | `gate` (`gate`, `status`) |
| compounding (accepted patch folded into baseline) | `baseline_advanced` |
| stop reason | `goal_met` / `stopped` / else max-rounds from `run_finished` |
| conclusion (pareto, accepts) | `run_finished` (`pareto`, `accepted`) |
| next-step agenda | `direction_proposed` (`id`, `direction`) minus any later `direction_resolved` (`id`, `status`) |

## Skeleton (render in the run's working language: English for this project)

1. **Operating flow**: one paragraph on how the loop ran (generator proposes → deterministic judge verifies correctness then significance → memory → compound), then the command and the actual steps (freeze baseline → A/A floor → per round: generate → build → test → differential → paired A/B → significance → record/compound → produce events.jsonl). This part is fixed explanation; the only numbers are the command and the bench metric.
2. **Noise floor (A/A measured)**: the `floors` table, plus the one-line meaning: same code, two runs, differs this much; a Δ smaller than the floor is luck, dropped.
3. **What each round did**: per candidate: id, verdict, hypothesis, files, and the Δ%/CI/floor/verdict table from its `deltas`.
4. **What was optimized / what was verified**: the list of attempted changes (verbatim hypotheses + verdicts), then the gates each candidate had to clear (guard → build → test → differential → paired A/B vs the A/A floor + bootstrap CI).
5. **Conclusion**: what entered the Pareto front (if anything); if nothing did, say so plainly: a low single-round hit rate is by design, gains compound over rounds. Note the within-noise NoOp (if present) as proof the gate is honest.
6. **Next research directions (open agenda)**: the still-open directions: every `direction_proposed` minus any later `direction_resolved`. For each, copy its `direction` and `rationale` verbatim (same no-laundering rule: do not invent or upgrade a direction). These are real, machine-stored items the next round will consume, not decoration. If none are open, say the agenda is clear.

Keep it to what the events support. A round that produced no accepted candidate is a normal, reportable outcome, not a failure to paper over.
