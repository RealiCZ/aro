"""Trajectory — the cumulative-improvement-over-attempts curve of a search run.

This is the artifact the autoresearch question turns on: does the search keep
finding wins (a staircase that climbs → divergent/infinite is justified) or does
it flatten (a plateau → the convergent map is the right product)?

A trajectory reduces a run's `events.jsonl` (the verbatim judge log) to that
staircase: each ACCEPTED candidate compounds the running speedup (an accept folds
into the baseline, so the next is measured on top — the same compounding the
engine does), each non-accept is a flat tick (cost spent, no gain). A convergent
run ends with the line STOPPING (dry / untried empty); a divergent run runs to the
budget. Each step carries its oracle REGIME (byte-identical vs relaxed) — a win
under a weakened oracle is a different kind of claim and must be drawn differently.

Pure / stdlib-only, so the plotter and the selftest never need cargo.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import runlog


@dataclass
class Step:
    i: int                 # attempt index (1-based) along the search
    label: str             # function / hypothesis (short)
    verdict: str
    delta_pct: float | None  # this attempt's measured Δ (negative = faster); None if no metric
    accepted: bool
    cum_pct: float         # cumulative COMPOUNDED improvement after this step (negative = faster)
    regime: str = "byte-identical"   # "byte-identical" (strong oracle) | "relaxed"

    @property
    def speedup_pct(self) -> float:
        """Cumulative speedup as a positive 'percent faster' (for an upward axis)."""
        return -self.cum_pct


@dataclass
class Trajectory:
    name: str
    steps: list = field(default_factory=list)
    converged: bool = False   # stopped at a map (dry / untried-empty) vs hit the budget ceiling

    @property
    def final_pct(self) -> float:
        return self.steps[-1].cum_pct if self.steps else 0.0

    @property
    def accepts(self) -> int:
        return sum(1 for s in self.steps if s.accepted)


def _run_attempts(run_dir) -> list:
    """One run dir's attempts in order as `(label, verdict, delta_pct, accepted)`.
    Handles both the per-target loop (`candidate_proposed`/`candidate_verdict`) and
    the `--attempt` / divergent driver (`attempt_finished`)."""
    evs = runlog.load_run(run_dir)
    hyp: dict = {}
    out = []
    for e in evs:
        ev = e.get("event")
        if ev == "candidate_proposed":
            hyp[e.get("id")] = (e.get("hypothesis") or "").strip()
        elif ev == "candidate_verdict":
            ds = e.get("deltas", []) or []
            impr = [d for d in ds if d.get("improved")]
            d = impr[0] if impr else (ds[0] if ds else None)
            dp = d.get("delta_pct") if d else None
            label = (hyp.get(e.get("id")) or e.get("id") or "").splitlines()[0][:40]
            out.append((label, e.get("verdict"), dp, e.get("verdict") == "accepted",
                        e.get("regime") or ""))
        elif ev == "attempt_finished":
            out.append(((e.get("fn") or "")[:40], e.get("verdict"),
                        e.get("delta"), bool(e.get("accepted")), e.get("regime") or ""))
    return out


def stitch(run_dirs, name: str, *, regime: str = "byte-identical",
           converged: bool = False) -> Trajectory:
    """Concatenate the attempts across `run_dirs` (in order) into one compounding
    trajectory. Accepted Δs multiply the running factor — exactly the engine's
    compounding — so the cumulative is the real stacked speedup, not a sum."""
    factor = 1.0
    steps = []
    i = 0
    for rd in run_dirs:
        for (label, verdict, dp, accepted, ev_regime) in _run_attempts(rd):
            i += 1
            if accepted and isinstance(dp, (int, float)):
                factor *= (1 + dp / 100.0)
            steps.append(Step(i=i, label=label, verdict=verdict, delta_pct=dp,
                              accepted=accepted, cum_pct=(factor - 1) * 100.0,
                              regime=(ev_regime or regime)))  # per-attempt regime wins
    return Trajectory(name=name, steps=steps, converged=converged)
