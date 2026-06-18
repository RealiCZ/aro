"""Structured event log — a machine-readable trace of the whole run.

Every meaningful step appends one JSON line to `events.jsonl` (flushed
immediately, so a watcher can tail it live — which also gives interim progress
for a backgrounded run). This is the feed a progress bot (B99 → Lark card, per
ARO-eng.md §1.6) consumes to report状态 without parsing logs.

Event vocabulary (the `event` field), mapped to the doc's §1.6 table:
  run_started        启动        — target, baseline, config
  baseline_built     启动        — the frozen baseline worktree is ready
  floors_calibrated  启动        — A/A noise floors
  round_started      里程碑/进度  — a new round begins (carries the memory it conditions on)
  candidate_proposed 进度        — generator produced a candidate (id, hypothesis, files)
  gate               进度        — one gate result (guard/apply/build/test/differential/significance)
  candidate_verdict  新优化/进度  — final verdict for a candidate (+ per-metric deltas)
  baseline_advanced  新优化进前沿 — an accepted patch was folded into the working baseline (#5 compounding)
  direction_proposed 进化方向    — the reflect step queued a new research direction onto the agenda
  direction_resolved 进化方向    — a prior agenda direction was marked done/dropped
  run_finished       收尾        — pareto front, totals, elapsed
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

# Fields too bulky to echo to the console (still written in full to the file).
_BULKY = {"deltas", "floors", "memory_summary"}


class EventLog:
    def __init__(self, path, also_console: bool = True):
        self.path = Path(path)
        self.seq = 0
        self.start = time.monotonic()
        self.also_console = also_console
        self.path.write_text("")  # truncate / create

    def emit(self, event: str, **fields) -> None:
        self.seq += 1
        rec = {
            "seq": self.seq,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": round(time.monotonic() - self.start, 3),
            "event": event,
        }
        rec.update(fields)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        if self.also_console:
            tail = " ".join(f"{k}={v}" for k, v in fields.items() if k not in _BULKY)
            print(f"[ev {rec['seq']:>3} {rec['elapsed_s']:>7.1f}s] {event}  {tail}", flush=True)
