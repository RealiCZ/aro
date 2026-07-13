"""Core data types for ARO. stdlib-only.

Vocabulary follows the design doc: a memory-driven directed loop with a **generator**
(proposes patches) and a separate **evaluator** (the two-gate
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
    lens: Optional[str] = None  # optimization lens/technique this candidate was framed under
                                # (micro / data-layout / algorithm) — recorded for the
                                # explore-mode "technique" coverage axis (re-run-proof, vs
                                # re-deriving it from the candidate id + the ladder formula)
    tokens: Optional[int] = None    # LLM output tokens the generator spent on this candidate
    cost_usd: Optional[float] = None  # ...and its $ cost. Both feed the perf-vs-cumulative-token
                                # trajectory chart (X = cumulative output tokens = real effort).
    category: str = "cpu"       # claim class for the Ir gate: "cpu" (default — Ir is final)
                                # or "locality"/"memory"/"cache" (may pass through to wall-clock
                                # significance when |ΔIr| ≤ ε and cache-miss evidence agrees)


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
    noise_limited: bool = False  # CI excludes 0 (consistent direction) but |Δ| < floor —
                                 # a real directional effect the measurement can't yet resolve
    bench_scale: int = 1   # the ARO_BENCH_SCALE this delta was measured at (auto-tightening)


class Verdict(str, Enum):
    REJECTED = "rejected"        # reward-hacking guard: reached outside the impl; never ran
    BUILD_FAILED = "build-failed"
    VERIFY_FAILED = "verify-failed"  # tests failed, or differential mismatch
    WITHIN_NOISE = "within-noise"    # no objective moved; CI consistent with zero
    NOISE_LIMITED = "noise-limited"  # a consistent directional effect (CI excludes 0) the
                                     # measurement can't resolve above its floor even after
                                     # auto-tightening — real but unprovable at achievable power
    REGRESSED = "regressed"
    ACCEPTED = "accepted"            # entered the Pareto front (wall-clock significance)
    # Instruction-count gate (Gate 1.5): deterministic Ir A/B, single-run final for
    # CPU-bound candidates. ACCEPTED_IR folds like ACCEPTED; NEUTRAL_IR discards with a
    # lesson ("compiler already did it" / zero product diff); REGRESSED_IR discards;
    # NO_COVERAGE means the probe does not exercise the patched files.
    ACCEPTED_IR = "accepted-ir"
    NEUTRAL_IR = "neutral-ir"
    REGRESSED_IR = "regressed-ir"
    NO_COVERAGE = "no-coverage"
    # Historical wall-clock claim closed by Ir gate or CodSpeed instruction-count
    # adjudication (append-only counter-record). CLOSED, not an accept.
    REFUTED_BY_ICOUNT = "refuted-by-icount"


def is_accept_verdict(v: Verdict) -> bool:
    """True for any verdict that advances the Pareto front / folds into the baseline."""
    return v in (Verdict.ACCEPTED, Verdict.ACCEPTED_IR)


@dataclass
class EvalOutcome:
    candidate_id: str
    verdict: Verdict
    deltas: list[MetricDelta] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # The semantic critic's structured rubric names (e.g. "layer-dissolve"),
    # carried verbatim so downstream consumers (lesson gating) never sniff
    # them back out of the freeform notes.
    critic_rubrics: list[str] = field(default_factory=list)
    # Additive Ir-gate evidence. Present when the candidate passed through Gate 1.5
    # (including locality passthrough into wall-clock Gate 2). None on paths that
    # never measured Ir (guard reject, build fail, MockTarget without icount, …).
    ir_delta_pct: Optional[float] = None
    profile_fingerprint: Optional[str] = None


@dataclass
class NoiseFloors:
    """A/A-calibrated per-metric noise floor (percentage magnitude)."""
    floors: dict[str, float] = field(default_factory=dict)

    def floor(self, metric: str) -> float:
        """Floor for a metric; falls back to 2.0% if uncalibrated."""
        return self.floors.get(metric, 2.0)

    def put(self, metric: str, floor_pct: float) -> None:
        self.floors[metric] = floor_pct


def _delta_field(d, name, default=None):
    """Shape-agnostic field access: a MetricDelta object or its dict form (an event's
    `deltas` entry / a stored record's `metrics` entry)."""
    return d.get(name, default) if isinstance(d, dict) else getattr(d, name, default)


def improvement(d, minimize: bool) -> float:
    """Direction-aware improvement of ONE delta (positive = better): for a minimize
    metric a more-negative Δ% is better; for maximize, more-positive."""
    dp = _delta_field(d, "delta_pct", 0.0) or 0.0
    return -dp if minimize else dp


def best_improvement(deltas, minimize_by: dict):
    """The objective delta with the LARGEST direction-aware improvement, as
    `(delta, improvement)`; None when `deltas` is empty. `minimize_by` maps
    metric → minimize (unknown metrics default to minimize). This is THE ranking
    rule — the engine folds round winners by it, the CLI and sweep record lessons
    by it — so 'which win was biggest' cannot disagree across artifacts."""
    best = None
    for d in deltas or []:
        imp = improvement(d, minimize_by.get(_delta_field(d, "metric"), True))
        if best is None or imp > best[1]:
            best = (d, imp)
    return best


def pick_reported_delta(deltas):
    """The delta to HEADLINE for a candidate when no objective map is at hand:
    among `improved`-flagged deltas the largest |Δ%| (the judge's improved flag is
    already direction-correct), else the first (= the primary objective). Returns
    the delta (object or dict) or None. Shared by store/manifest/trajectory so the
    same run never shows different headline numbers per artifact."""
    ds = list(deltas or [])
    if not ds:
        return None
    improved = [d for d in ds if _delta_field(d, "improved")]
    if improved:
        return max(improved, key=lambda d: abs(_delta_field(d, "delta_pct", 0.0) or 0.0))
    return ds[0]


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
    base_edits: list = field(default_factory=list)  # cumulative accepted patch (agentic builds on it)
    emit: Optional[object] = None  # events.emit hook — generators report failures through it
                                   # (generator_error) instead of silently yielding nothing


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
    folded_edits: list = field(default_factory=list)  # edits THIS run actually compounded
                                                      # into the baseline (past the seed) —
                                                      # the meta-loop's new cumulative wins
