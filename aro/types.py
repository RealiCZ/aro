"""Core data types for ARO. stdlib-only.

Vocabulary follows ARO-eng.md: a memory-driven directed loop with a **generator**
(proposes patches) and a separate **evaluator / 评判器** (the two-gate
verification: correctness then significance).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class Edit:
    """A single code edit: replace `search` with `replace` in `path` (repo-relative)."""
    path: str
    search: str
    replace: str


@dataclass
class Patch:
    """What the generator produces. An empty edit list is the `NoOp` control:
    byte-identical to the frozen baseline. It must build, pass tests, pass the
    differential check, and land *within noise* — proving the gate manufactures
    no false positives."""
    edits: list[Edit] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return len(self.edits) == 0

    @staticmethod
    def noop() -> "Patch":
        return Patch(edits=[])


@dataclass
class Objective:
    """One optimization objective; `minimize` means a smaller value is better."""
    metric: str
    minimize: bool = True


@dataclass
class Candidate:
    id: str
    hypothesis: str
    patch: Patch
    parent: Optional[str] = None


@dataclass
class Metrics:
    """Measured bench result: metric name -> raw samples (criterion ns/iter)."""
    samples: dict[str, list[float]] = field(default_factory=dict)

    def put(self, metric: str, values: list[float]) -> None:
        self.samples[metric] = values

    def get(self, metric: str) -> Optional[list[float]]:
        return self.samples.get(metric)

    def metric_names(self) -> list[str]:
        return list(self.samples.keys())

    @property
    def is_empty(self) -> bool:
        return not self.samples


@dataclass
class MetricDelta:
    """Per-metric significance result (paired A/B + A/A floor + bootstrap CI)."""
    metric: str
    baseline: float
    candidate: float
    delta_pct: float       # (cand - base)/base * 100; negative = faster/smaller
    ci_low_pct: float
    ci_high_pct: float
    floor_pct: float       # A/A-calibrated noise floor (magnitude, %)
    improved: bool         # beyond floor AND CI excludes 0 on the good side
    regressed: bool


class Verdict(str, Enum):
    REJECTED = "rejected"        # reward-hacking guard: reached outside the impl; never ran
    BUILD_FAILED = "build-failed"
    VERIFY_FAILED = "verify-failed"  # tests failed, or differential mismatch
    WITHIN_NOISE = "within-noise"    # no objective improved beyond its floor
    REGRESSED = "regressed"
    ACCEPTED = "accepted"            # entered the Pareto front


@dataclass
class EvalOutcome:
    candidate_id: str
    verdict: Verdict
    deltas: list[MetricDelta] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class NoiseFloors:
    """A/A-calibrated per-metric noise floor (percentage magnitude)."""
    floors: dict[str, float] = field(default_factory=dict)

    def floor(self, metric: str) -> float:
        """Floor for a metric; falls back to 2.0% if uncalibrated."""
        return self.floors.get(metric, 2.0)

    def put(self, metric: str, floor_pct: float) -> None:
        self.floors[metric] = floor_pct


@dataclass
class Direction:
    """One open research direction in the agenda — what to try next and why,
    derived by the reflect step from a round's verdicts. This is generation-side
    (a hypothesis worth pursuing), never a judgement: the deterministic evaluator
    still decides whether pursuing it actually wins. The agenda is how the loop
    accumulates *forward* direction, not just a list of dead ends to avoid."""
    id: str
    direction: str        # one line: what to try next
    rationale: str        # why — grounded in a verdict / measurement
    source: str           # "reflect-rN" or an originating candidate id
    status: str = "open"  # open | done | dropped
    round: int = 0


@dataclass
class GenContext:
    """Context handed to the generator each round (the memory it conditions on)."""
    round: int
    objectives: list[Objective]
    baseline: Metrics
    memory_summary: str
    region_hint: Optional[str] = None
    plan: Optional[str] = None  # output of the read phase: what to implement this round
    agenda: list = field(default_factory=list)  # open Directions carried from reflect


@dataclass
class Report:
    """The engine's end-of-run snapshot (pure data — no rendering).

    The human RUN-REPORT.md is rendered from `events.jsonl` by the `aro` skill's
    report flow (see skill/references/report-protocol.md), NOT by Python: report
    prose stays out of code, and every number is copied verbatim from the event
    log rather than re-narrated. This object exists only so callers (CLI tail,
    selftest, verify) can read structured totals without parsing the log."""
    target: str
    baseline_ref: str
    rounds: int
    floors: NoiseFloors
    outcomes: list = field(default_factory=list)   # list of (Candidate, EvalOutcome)
    pareto: list = field(default_factory=list)
    log: list = field(default_factory=list)
    elapsed_secs: float = 0.0
