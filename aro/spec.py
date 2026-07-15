"""TargetSpec — a declarative description of an optimization target.

This is how ARO generalizes: a new repo is a new spec file (in `targets/`), not
new Python. The *authored* file is the **7-slot** contract — the human-readable
"what are we optimizing, and how do we know a win is real":

    target_repo · hot_path · metric · direction
    benchmark_probe · correctness_oracle · constraints      (+ a `run` block of knobs)

`load()` normalizes that into the flat working fields the driver/judge consume
(bench/build/test/regions/context/profile/objectives/goal/stop/...), so the
authored format stays clean while the internals don't churn. JSON (Python
3.9-safe; no tomllib). Paths inside the spec (probes, prompts) are resolved
relative to the aro-py repo root; `repo` is resolved as a filesystem path.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent  # aro-py/

_DEFAULT_PROMPTS = {"agentic": "agentic", "hint": "hint", "hint_blind": "hint_blind"}


@dataclass
class Goal:
    metric: str
    direction: str = "minimize"          # minimize | maximize
    target: Optional[float] = None       # absolute target value; None = open-ended/best-effort


@dataclass
class Stop:
    max_rounds: int = 3                  # hard cap on rounds
    dry_rounds: int = 2                  # stop after this many consecutive non-accepts (diminishing returns)


@dataclass
class TargetSpec:
    """The normalized working form. Authored as 7 slots (see module docstring);
    these fields are what the driver and judge actually read."""
    name: str
    repo: Path
    baseline_ref: str
    build: list                          # command token list, e.g. ["cargo","build","--release","-p","<crate>"]
    test: list
    bench: dict                          # {probe, example, pkg, sample_prefix, metric}
    profile: dict                        # {example, spin_secs, sample_secs}
    regions: list                        # editable files (the guard rejects edits outside these)
    context: dict                        # {file, anchors:[[kind,name],...]}
    objectives: list                     # [{metric, minimize}]
    goal: Goal
    stop: Stop
    prompts: dict                        # {agentic, hint, hint_blind}
    llm_backend: str = "claude"          # generation backend; env ARO_LLM_BACKEND wins
    critic_backend: Optional[str] = None  # optional cross-model semantic reviewer
    generator: str = "agentic"           # "agentic" (heavy, default) | "ralph" (thin)
    differential: dict = field(default_factory=dict)  # {probe,pkg,example,prefix}; empty → verify-failed unless constraints.weak_oracle
    timeout: int = 1800                  # per build/test/bench/probe subprocess (s) — guards hangs
    aa_runs: int = 2                     # A/A calibration runs (CLI --aa-runs overrides)
    ab_pairs: int = 4                    # paired A/B count (CLI --ab-pairs overrides)
    bench_scales: tuple = (1, 8, 64)     # auto-tighten: on a noise-limited verdict, re-bench
                                         # at the next ARO_BENCH_SCALE to drop the floor (bounded)
    read_phase: bool = True
    blind: bool = False
    classify: dict = field(default_factory=dict)      # {runtime:[...], crypto:[...]}: extends the builtin owner-label lists
    constraints: dict = field(default_factory=dict)   # {editable, no_new_deps, byte_identical, notes}
    # Ir-gate (Gate 1.5) configuration. probe_covers is optional: when absent the
    # gate warns and proceeds (backward compatible); when present a candidate whose
    # patched files share no prefix is NO_COVERAGE. ε defaults to 0.1% — Ir is
    # deterministic; ε only guards residual environment nondeterminism (env
    # ARO_ICOUNT_EPSILON wins over the JSON field).
    probe_covers: list = field(default_factory=list)
    icount_epsilon_pct: float = 0.1
    # Terminal criterion-Ir gate (pre-PR, plan §4). When terminal_bench_targets is
    # non-empty, mergeable requires TERMINAL_CONFIRMED. measure_bin is the
    # server-side path to mega-bench-reporter (env ARO_MEASURE_BIN wins).
    terminal_bench_targets: list = field(default_factory=list)
    terminal_bench_filter: Optional[str] = None
    measure_bin: Optional[str] = None
    # Upstream control lanes in the criterion bench (do not execute candidate
    # code). When non-empty, terminal adjudication excludes them from
    # improved/regressed and judges |Δ%| against control_composition_bound_pct.
    control_lanes: list = field(default_factory=list)
    control_composition_bound_pct: Optional[float] = None
    # Outlier quarantine (manifest): |Δ| above this % → mergeable=false until
    # human review. DEFAULT 5.0 EVEN WHEN ABSENT — deliberately breaks the usual
    # "absent field = legacy off" convention; a quarantine nobody declares
    # protects nobody. Explicit 0 disables the tripwire.
    outlier_quarantine_pct: float = 5.0
    raw: dict = field(default_factory=dict)

    def probe_src(self) -> str:
        return (REPO_ROOT / self.bench["probe"]).read_text()

    def diff_probe_src(self) -> str:
        return (REPO_ROOT / self.differential["probe"]).read_text()

    @staticmethod
    def from_dict(d: dict) -> "TargetSpec":
        """Normalize a 7-slot authored dict. Thin alias of module-level from_dict."""
        return from_dict(d)


def spec_field(spec, name, default=None, *, cast=None, validate=None):
    """Resolve `name` from a TargetSpec: attribute → raw dict → default.

    Precedence matches the historical resolve_* copies in terminal.py:
    1. `getattr(spec, name)` when not None
    2. else `(spec.raw or {}).get(name)`
    3. else `default`

    A blank string (after strip) at either layer counts as unset and falls
    through to default. Optional `cast` is applied only to a resolved
    non-default value; optional `validate(value)` may raise.
    """
    if spec is None:
        return default
    v = getattr(spec, name, None)
    if v is None:
        raw = getattr(spec, "raw", None) or {}
        v = raw.get(name)
    if v is None or str(v).strip() == "":
        return default
    if cast is not None:
        v = cast(v)
    if validate is not None:
        validate(v)
    return v


def load(path) -> TargetSpec:
    spec = from_dict(json.loads(Path(path).read_text()))
    validate_artifacts(spec)
    return spec


def validate_artifacts(spec: TargetSpec) -> None:
    """LOAD-time checks beyond key presence, so a broken spec fails in seconds with
    the slot named instead of mid-run after real money is spent. Deliberately NOT in
    from_dict: programmatic/test construction stays pure; this runs on the `load()`
    path every CLI entry uses.

    - Probe FILES must exist (a typo'd path otherwise surfaces as a raw
      FileNotFoundError from probe_src() at first bench).
    - The editable region must be non-empty: an empty list would silently DISABLE
      the guard's region check (guard.screen short-circuits on falsy regions)
      rather than tighten it — the opposite of what an author would expect.
    - hot_path.fn is advisory (attempt mode retargets per function), so a missing
      fn only WARNS: the seed/context hint is stale, not the run broken."""
    probe = REPO_ROOT / spec.bench["probe"]
    if not probe.exists():
        raise SpecError(f"benchmark_probe.probe: no file at {probe} "
                        f"(probe paths are relative to the aro repo root)")
    if spec.differential:
        dprobe = REPO_ROOT / spec.differential["probe"]
        if not dprobe.exists():
            raise SpecError(f"correctness_oracle.differential.probe: no file at {dprobe} "
                            f"(probe paths are relative to the aro repo root)")
    if not spec.regions:
        raise SpecError("empty editable region: set constraints.editable (files or "
                        "directories) or hot_path.file — an empty region list silently "
                        "disables the edit-region guard instead of tightening it")
    f = spec.context.get("file")
    fn = next((a[1] for a in (spec.context.get("anchors") or [])
               if len(a) == 2 and a[0] == "fn"), None)
    if f and fn:
        p = spec.repo / f
        try:
            if p.exists() and not re.search(r"\bfn\s+" + re.escape(fn) + r"\b",
                                            p.read_text()):
                print(f"WARNING: hot_path.fn `{fn}` not found in {f} — the seed hint "
                      f"is stale (advisory: attempt mode retargets per function)")
        except Exception:
            pass


class SpecError(ValueError):
    """A spec is missing a required slot/key — raised at LOAD time with the exact
    slot named, instead of a bare KeyError deep inside target.bench mid-run."""


def _require(blk: dict, slot: str, *keys):
    missing = [k for k in keys if not blk.get(k)]
    if missing:
        raise SpecError(f"spec slot '{slot}' is missing required key(s): "
                        f"{', '.join(missing)}")


def _validate(d: dict) -> None:
    _require(d, "(top level)", "name", "target_repo", "metric",
             "benchmark_probe", "correctness_oracle")
    _require(d["target_repo"], "target_repo", "path")
    _require(d["benchmark_probe"], "benchmark_probe", "probe", "example", "pkg")
    oracle = d["correctness_oracle"]
    _require(oracle, "correctness_oracle", "build", "test")
    for k in ("build", "test"):
        if not isinstance(oracle[k], list):
            raise SpecError(f"spec slot 'correctness_oracle.{k}' must be a command "
                            f"token list, got {type(oracle[k]).__name__}")
    diff = oracle.get("differential")
    if diff:
        _require(diff, "correctness_oracle.differential", "probe", "pkg", "example", "prefix")


def from_dict(d: dict) -> TargetSpec:
    """Normalize a 7-slot spec dict into a TargetSpec. Missing optional slots fall
    back to sane defaults; the four required slots are target_repo, metric,
    benchmark_probe, correctness_oracle — validated HERE, so a broken spec fails
    at load with the slot named, not as a KeyError mid-run."""
    _validate(d)
    repo_blk = d["target_repo"]
    repo = Path(repo_blk["path"]).expanduser().resolve()
    baseline_ref = repo_blk.get("baseline_ref", "HEAD")

    hot = d.get("hot_path", {})
    metric = d["metric"]
    direction = d.get("direction", "minimize")

    bp = d["benchmark_probe"]
    prof = bp.get("profile", {})
    cargo_args = bp.get("cargo_args") or []
    if not isinstance(cargo_args, list):
        raise SpecError("spec slot 'benchmark_probe.cargo_args' must be a command "
                        f"token list (e.g. [\"--features\", \"foo\"]), got "
                        f"{type(cargo_args).__name__}")
    bench = {
        "probe": bp["probe"], "example": bp["example"], "pkg": bp["pkg"],
        "sample_prefix": bp.get("sample_prefix", "BENCH"), "metric": metric,
        # Extra cargo flags for every probe/example build+run (e.g. --features):
        # the oracle's build/test commands are user-authored and carry their own,
        # but the probe invocations were hardcoded and could not follow a
        # feature-gated hot path (measured default-feature codegen instead).
        "cargo_args": [str(a) for a in cargo_args],
    }
    profile = {"example": bp["example"],
               "spin_secs": prof.get("spin_secs", 8),
               "sample_secs": prof.get("sample_secs", 4)}

    oracle = d["correctness_oracle"]
    build = oracle["build"]
    test = oracle["test"]
    differential = oracle.get("differential", {})

    classify = d.get("classify", {})
    if not isinstance(classify, dict) or any(
            not isinstance(v, list) for v in classify.values()):
        raise SpecError("spec slot 'classify' must be a dict of lists, e.g. "
                        "{\"runtime\": [\"tokio\"], \"crypto\": [\"ring\"]}")

    constraints = d.get("constraints", {})
    regions = constraints.get("editable") or ([hot["file"]] if hot.get("file") else [])
    context = {"file": hot.get("file"),
               "anchors": [["fn", hot["fn"]]] if hot.get("fn") else []}

    # Objectives: the (metric, direction) pair is canonical (single-objective). A
    # multi-objective target may still pass an explicit `objectives` list to guard a
    # second metric; the goal stays the primary (metric, direction).
    objectives = d.get("objectives") or [
        {"metric": metric, "minimize": direction == "minimize"}]

    run = d.get("run", {})
    stop_blk = run.get("stop", {})
    return TargetSpec(
        name=d["name"],
        repo=repo,
        baseline_ref=baseline_ref,
        build=build,
        test=test,
        bench=bench,
        profile=profile,
        regions=regions,
        context=context,
        objectives=objectives,
        goal=Goal(metric=metric, direction=direction, target=run.get("goal_target")),
        stop=Stop(max_rounds=stop_blk.get("max_rounds", 3),
                  dry_rounds=stop_blk.get("dry_rounds", 2)),
        prompts=run.get("prompts", _DEFAULT_PROMPTS),
        llm_backend=str(d.get("llm_backend") or "claude"),
        critic_backend=(str(d["critic_backend"]) if d.get("critic_backend") else None),
        generator=run.get("generator", "agentic"),
        differential=differential,
        timeout=run.get("timeout", 1800),
        aa_runs=run.get("aa_runs", 2),
        ab_pairs=run.get("ab_pairs", 4),
        bench_scales=tuple(run.get("bench_scales", (1, 8, 64))),
        read_phase=run.get("read_phase", True),
        blind=run.get("blind", False),
        classify=classify,
        constraints=constraints,
        probe_covers=list(d.get("probe_covers") or []),
        icount_epsilon_pct=float(d.get("icount_epsilon_pct", 0.1)),
        terminal_bench_targets=list(d.get("terminal_bench_targets") or []),
        terminal_bench_filter=(str(d["terminal_bench_filter"])
                               if d.get("terminal_bench_filter") else None),
        measure_bin=(str(d["measure_bin"]) if d.get("measure_bin") else None),
        control_lanes=list(d.get("control_lanes") or []),
        control_composition_bound_pct=(
            float(d["control_composition_bound_pct"])
            if d.get("control_composition_bound_pct") is not None
            else (2.0 if d.get("control_lanes") else None)),
        # Default 5.0 even when absent (see field docstring). Explicit 0 = off.
        outlier_quarantine_pct=(
            float(d["outlier_quarantine_pct"])
            if d.get("outlier_quarantine_pct") is not None
            else 5.0),
        raw=d,
    )
