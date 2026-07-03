"""runlog — the single READER for a run's `events.jsonl` (the machine-readable truth).

Writing stays in `aro/events.py` (EventLog). Every consumer that reads events back —
manifest, tree, chart, trajectory, sweep's finalize — goes through here, so the two
load-bearing rules exist exactly once:

  1. **Parsing**: one JSON object per line; a malformed/blank line is skipped, never fatal.
  2. **The latest-run slice**: the log is append-only across re-runs into the same
     `--out`, each line stamped with its writer's `run_id`. THE LATEST RUN is the
     `run_id` of the LAST line that carries one (append-only ⇒ the last line belongs
     to the most recent writer). If no line carries a run_id, the whole file is
     treated as one run.

Before this module, three subtly different slice rules lived in manifest/tree/
trajectory (trajectory keyed off `run_started` events only — a run that crashed
before emitting `run_started` would silently re-render the PREVIOUS run). One rule,
one place.
"""
from __future__ import annotations

import json
from pathlib import Path

# --- the event vocabulary (wire names consumers match on) -------------------------
# Producers: aro/engine.py, aro/eval.py, aro/sweep.py, aro/generator.py (via events).
RUN_STARTED = "run_started"
RUN_FINISHED = "run_finished"
BASELINE_BUILT = "baseline_built"
BASELINE_RESUMED = "baseline_resumed"
BASELINE_ADVANCED = "baseline_advanced"
BASELINE_PROFILED = "baseline_profiled"
REGRESSION_BASELINE = "regression_baseline"
FLOORS_CALIBRATED = "floors_calibrated"
ROUND_STARTED = "round_started"
READ_PHASE = "read_phase"
CANDIDATE_PROPOSED = "candidate_proposed"
CANDIDATE_VERDICT = "candidate_verdict"
CANDIDATE_SUPERSEDED = "candidate_superseded"
GATE = "gate"
BENCH_RESCALED = "bench_rescaled"
CRITIC = "critic"
CRITIC_ERROR = "critic_error"
PRESCREEN = "prescreen"
PRESCREEN_ORDERED = "prescreen_ordered"
REFLECT = "reflect"
DIRECTION_PROPOSED = "direction_proposed"
DIRECTION_RESOLVED = "direction_resolved"
GOAL_MET = "goal_met"
STOPPED = "stopped"
ERROR = "error"
GENERATOR_ERROR = "generator_error"
ATTEMPT_FRONTIER = "attempt_frontier"
PROFILE_FLOOR = "profile_floor"
ATTEMPT_STARTED = "attempt_started"
ATTEMPT_SKIPPED = "attempt_skipped"
ATTEMPT_ERRORED = "attempt_errored"
ATTEMPT_FINISHED = "attempt_finished"
ATTEMPT_RESWEEP = "attempt_resweep"
ATTEMPT_EXHAUSTED = "attempt_exhausted"
EXPLORE_STEP = "explore_step"
EXPLORE_STOP = "explore_stop"


def read_events(path) -> list:
    """All events from an `events.jsonl` (or a run dir containing one), in file
    order. Malformed / blank lines are skipped. [] when the file doesn't exist."""
    p = Path(path)
    if p.is_dir():
        p = p / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def latest_slice(evs: list) -> list:
    """The canonical latest-run slice (rule 2 in the module docstring)."""
    rids = [e.get("run_id") for e in evs if e.get("run_id")]
    if not rids:
        return evs
    last = rids[-1]
    return [e for e in evs if e.get("run_id") == last]


def load_run(path) -> list:
    """`read_events` + `latest_slice`: the latest run's events from a run dir."""
    return latest_slice(read_events(path))
