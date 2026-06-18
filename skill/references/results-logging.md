# Results logging & memory

What ARO persists, where, and the schema — so a run is resumable, auditable, and a progress bot can sync state. Everything lives under the run's `--out` dir. Code: `aro/store.py` (memory) + `aro/events.py` (event stream).

## Files

| file | schema | role |
|---|---|---|
| `records.jsonl` | one JSON object per candidate: `{id, verdict, hypothesis, metrics:[{metric,delta_pct,ci_low_pct,ci_high_pct,floor_pct,improved,regressed}], notes}` | the result ledger; the next round reads it (dead ends, Pareto) |
| `floors.json` | `{metric: floor_pct}` | A/A-calibrated noise floors |
| `pareto.txt` | accepted candidate ids, one per line | the accepted front |
| `agenda.jsonl` | one Direction per line: `{id, direction, rationale, source, status, round}` | the forward-looking research agenda — reflect adds directions, the next round reads the open ones |
| `patches/<id>.txt` | the patch (NoOp or `<<<SEARCH/REPLACE>>>` blocks) | full provenance of every candidate |
| `events.jsonl` | one flushed JSON line per step (below) | live, machine-readable feed for a progress bot; `tail -f` for progress |
| `RUN-REPORT.md` | **skill-rendered from `events.jsonl`** | the human narrative — NOT written by Python; the report flow (`references/report-protocol.md`) copies every number (Δ/CI/floor/verdict) verbatim and never re-judges |

## Event vocabulary (`events.jsonl`)

Each line: `{seq, ts, elapsed_s, event, ...}`. Events: `run_started`, `baseline_built`, `regression_baseline` (`n_pre`), `floors_calibrated`, `round_started`, `read_phase` (`has_plan`), `candidate_proposed` (`id`, `hypothesis`, `files`), `gate` (`gate∈{guard,apply,build,test,regression,differential,significance}`, `status`), `candidate_verdict` (`verdict`, `deltas`), `baseline_advanced` (compounding), `direction_proposed` (`id`, `direction`) / `direction_resolved` (`id`, `status`) — the reflect agenda, `goal_met` / `stopped` (`reason`), `run_finished`.

This is the feed a progress bot (e.g. B99 → Lark card) consumes; it is also why a backgrounded run is observable without parsing logs.

## Resumability & compounding

`store.Memory.open(dir)` reloads `records.jsonl` / `pareto.txt` / `floors.json`, so re-running into the same `--out` continues from prior state (dead ends fed into the next prompt). An **accepted** patch folds into the working baseline (`baseline_advanced`), so subsequent rounds are generated and measured on top of it — gains compound. Use a fresh `--out` to start clean.

## Discipline (borrowed from autoresearch)

One change per round + immediate, append-only recording = fine-grained provenance and reproducibility. "Looks better" is never recorded as a win — only a judged verdict is.
