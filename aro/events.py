"""Structured event log — a machine-readable trace of the whole run.

Every meaningful step appends one JSON line to `events.jsonl` (flushed
immediately, so a watcher can tail it live — which also gives interim progress
for a backgrounded run). This is the feed a progress bot (B99 → Lark card, per
the design doc §1.6) consumes to report status without parsing logs.

Event vocabulary (the `event` field), mapped to the doc's §1.6 table:
  run_started        setup      - target, baseline, config
  baseline_built     setup      - the frozen baseline worktree is ready
  floors_calibrated  setup      - A/A noise floors
  round_started      progress   - a new round begins (carries the memory it conditions on)
  candidate_proposed progress   - generator produced a candidate (id, hypothesis, files)
  gate               progress   - one gate result (guard/apply/build/test/differential/significance)
  bench_rescaled     progress   - a noise-limited objective triggered a re-bench at a higher ARO_BENCH_SCALE
  candidate_verdict  verdict    - final verdict for a candidate (+ per-metric deltas; may be noise-limited)
  baseline_advanced  win        - an accepted patch was folded into the working baseline (#5 compounding)
  direction_proposed agenda     - the reflect step queued a new research direction onto the agenda
  direction_resolved agenda     - a prior agenda direction was marked done/dropped
  run_finished       teardown   - pareto front, totals, elapsed

Envelope (every line): seq (monotonic order), run_id (the run; a report renders the
latest run_id's slice), ts, elapsed_s, event. During a sweep's per-function backtest an
`attempt` field is also stamped (the a<N> dir index) so an event maps to its attempt dir
without timeline-counting — see `EventLog.context` and `aro/manifest.py`.
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

# Fields too bulky to echo to the console (still written in full to the file).
_BULKY = {"deltas", "floors", "memory_summary"}


class EventLog:
    def __init__(self, path, also_console: bool = True, run_id=None):
        self.path = Path(path)
        self.seq = 0
        self.start = time.monotonic()
        self.also_console = also_console
        # Each run gets an id; the log is APPENDED, never truncated, so re-running
        # into the same --out keeps the prior run's events (the truth source isn't
        # lost). A report renders the latest run_id's slice.
        self.run_id = run_id or datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        # Ambient fields stamped onto EVERY event until changed — e.g. `attempt` (the
        # a<N> dir index) during a sweep's per-function backtest. Without this, all of a
        # sweep's attempts share one run_id and candidate ids collide across attempts
        # (`agent-r0-0` exists in every a<N>), so a consumer can't map an event to its
        # attempt dir without counting attempt_started. The stamp makes that linkage
        # explicit. The driver sets/clears it around each attempt (sweep.attempt).
        self.context: dict = {}
        if not self.path.exists():
            self.path.write_text("")  # create only

    def emit(self, event: str, **fields) -> None:
        self.seq += 1
        rec = {
            "seq": self.seq,
            "run_id": self.run_id,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": round(time.monotonic() - self.start, 3),
            "event": event,
        }
        rec.update(self.context)   # ambient (e.g. attempt); explicit fields below win
        rec.update(fields)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        if self.also_console:
            tail = " ".join(f"{k}={v}" for k, v in fields.items() if k not in _BULKY)
            print(f"[ev {rec['seq']:>3} {rec['elapsed_s']:>7.1f}s] {event}  {tail}", flush=True)
