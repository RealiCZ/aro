"""Terminal criterion-Ir gate (pre-PR).

Probe-level Ir wins do not imply criterion bench wins (coverage and weights
differ). Before opening a perf PR, measure both the baseline and candidate
worktrees via the external `mega-bench-reporter measure --instructions` CLI
and diff every row's instruction count — the same signal CodSpeed CI reports.

Intercepts the #326/#332 failure shape: a probe Ir win that moves zero
criterion rows must never become a PR. Spec: ARO_ICOUNT_GATE_PLAN §4.

Noise model (server-measured): each criterion row is its own process with a
fresh hasher seed, so single-iteration rows drift 0.01–1% run-to-run. The gate
is therefore noise-aware: each side is measured median-of-N times, and per-row
classification uses calibrated floors (or a conservative default) rather than
the inner probe-level ε. The probe Ir gate (ε=0.1%) is untouched.

The measure binary is not available on all hosts (macOS / no valgrind). Tests
inject a `runner` callable that returns fixture JSON; production uses the real
CLI. `ARO_MEASURE_BIN` wins over the target JSON `measure_bin` field.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .icount import ir_epsilon_pct
from .spec import spec_field
from .stats import median as _median

# --- verdict vocabulary (pre-PR terminal gate; not evaluate() outcomes) ------
# Single registry: satellites (chart/store/permtree) derive maps/sets from here.
# Candidate-level verdicts (accepted, within-noise, …) stay in their own modules.

TERMINAL_CONFIRMED = "TERMINAL_CONFIRMED"   # ≥1 row improved, none regressed beyond floor
TERMINAL_CONFIRMED_WITH_TRADE = "TERMINAL_CONFIRMED_WITH_TRADE"  # net win + tradeable regressions ≤ cap
TERMINAL_UNTOUCHED = "TERMINAL_UNTOUCHED"   # every row |Δ| ≤ floor → block PR (#326/#332)
TERMINAL_REGRESSED = "TERMINAL_REGRESSED"   # ≥1 row worse beyond floor, none improved
TERMINAL_MIXED = "TERMINAL_MIXED"           # improvements AND regressions → ablate → prune → re-terminal (see decision table)
TERMINAL_TEST_FAILED = "TERMINAL_TEST_FAILED"  # correctness_oracle.test_full failed; no measure
TERMINAL_CONTROL_ANOMALY = "TERMINAL_CONTROL_ANOMALY"  # control lane |Δ%| > composition bound
# Entry-level marker only (not a measured verdict): apply_terminal sets the flat
# `terminal` field to this when the entry's order is outside the measured set.
# Never appears as a top-level terminal.json verdict; never in TERMINAL_VERDICT_META.
TERMINAL_NOT_MEASURED = "TERMINAL_NOT_MEASURED"

# Ordered map: verdict → metadata. mergeable True only for CONFIRMED / WITH_TRADE.
# Optional `next` is a work-order hint printed on stderr by the CLI (see decision table
# in docs/OPERATIONS.md §13.6 / skill/references/run-to-pr.md). Absent or None = no line.
TERMINAL_VERDICT_META: dict = {
    TERMINAL_CONFIRMED: {
        "closed": True,
        "dead_end": False,
        "color": "#6A9F6A",
        "label": "terminal Ir confirmed",
        "mergeable": True,
    },
    TERMINAL_CONFIRMED_WITH_TRADE: {
        "closed": True,
        "dead_end": False,
        "color": "#7BAF7B",
        "label": "terminal Ir confirmed with trade",
        "mergeable": True,
    },
    TERMINAL_UNTOUCHED: {
        "closed": True,
        "dead_end": True,
        "color": "#A9B6C2",
        "label": "terminal Ir untouched (block PR)",
        "mergeable": False,
    },
    TERMINAL_REGRESSED: {
        "closed": True,
        "dead_end": True,
        "color": "#DD9580",
        "label": "terminal Ir regressed",
        "mergeable": False,
    },
    TERMINAL_MIXED: {
        "closed": True,
        "dead_end": True,
        "color": "#CBA255",
        "label": "terminal Ir mixed",
        "mergeable": False,
        "next": (
            "aro ablate — attribute per-entry marginals, drop offenders, "
            "re-run terminal"
        ),
    },
    TERMINAL_TEST_FAILED: {
        "closed": True,
        "dead_end": True,
        "color": "#DD9580",
        "label": "terminal full-suite test failed",
        "mergeable": False,
    },
    TERMINAL_CONTROL_ANOMALY: {
        "closed": True,
        "dead_end": True,
        "color": "#DD9580",
        "label": "terminal control-lane composition anomaly",
        "mergeable": False,
        "next": "A/A disambiguation before anything else — see OPERATIONS",
    },
}


def emit_verdict_next(verdict, *, file=None) -> None:
    """Print the prescribed next action for a terminal verdict, if any.

    Goes to **stderr** so stdout (verdict summary / JSON paths scripts parse)
    stays byte-stable. One line: `next: <meta.next>`. No-op when meta has no
    `next` (CONFIRMED, REGRESSED, UNTOUCHED, …).
    """
    meta = TERMINAL_VERDICT_META.get(str(verdict or ""))
    if not meta:
        return
    nxt = meta.get("next")
    if nxt:
        print(f"next: {nxt}", file=file if file is not None else sys.stderr)

ALL_TERMINAL_VERDICTS = frozenset(TERMINAL_VERDICT_META)
TERMINAL_CLOSED_VERDICTS = frozenset(
    v for v, m in TERMINAL_VERDICT_META.items() if m["closed"])
TERMINAL_DEAD_END_VERDICTS = frozenset(
    v for v, m in TERMINAL_VERDICT_META.items() if m["dead_end"])
TERMINAL_MERGEABLE_VERDICTS = frozenset(
    v for v, m in TERMINAL_VERDICT_META.items() if m["mergeable"])
TERMINAL_CHART_STYLES = {
    v: (m["color"], m["label"]) for v, m in TERMINAL_VERDICT_META.items()
}


def is_mergeable_terminal_verdict(verdict) -> bool:
    """True when a stamped terminal verdict unlocks mergeable (registry-driven)."""
    return str(verdict or "") in TERMINAL_MERGEABLE_VERDICTS

# Cap the manifest's nonzero-Δ summary so PR bodies stay short.
_MAX_BENCH_IR_ROWS = 32

# Floor calibration: max pairwise |Δ%| × safety, clamped to probe ε minimum.
FLOOR_SAFETY_FACTOR = 2.0
DEFAULT_TERMINAL_ROUNDS = 3
DEFAULT_TERMINAL_FLOOR_PCT = 1.0
DEFAULT_CALIBRATE_ROUNDS = 4
FLOORS_STALE_DAYS = 30
# Full-suite correctness tier at the terminal gate (optional test_full).
DEFAULT_TEST_FULL_TIMEOUT_SECS = 1800
_TEST_FULL_OUTPUT_TAIL = 2000
# Upstream control-lane composition drift bound (codegen inlining shifts).
DEFAULT_CONTROL_COMPOSITION_BOUND_PCT = 2.0

REPO_ROOT = Path(__file__).resolve().parent.parent

class TerminalError(Exception):
    """Hard failure of the terminal gate (config drift, missing tools, bad JSON).

    Never a verdict — the operator must fix the environment / row set / binary
    path before re-running. Distinct from TERMINAL_* outcomes.
    """


@dataclass
class MeasureDoc:
    """Parsed stdout of `mega-bench-reporter measure --instructions`."""
    rows: dict          # row_key -> instr_count (int)
    meta: dict = field(default_factory=dict)
    profile_fingerprint: str = ""
    rustc: str = ""


@dataclass
class RowDelta:
    row_key: str
    base_ir: int
    cand_ir: int
    delta_pct: float
    status: str         # improved | untouched | regressed | control-ok | control-anomaly
    floor_pct: float = 0.1


@dataclass
class TerminalResult:
    """Outcome of one baseline-vs-candidate terminal measurement."""
    verdict: str
    bench_ir_rows: dict          # row_key -> delta_pct for nonzero Δ (capped)
    profile_fingerprint: str
    rows: list = field(default_factory=list)   # list[RowDelta]
    notes: list = field(default_factory=list)
    epsilon_pct: float = 0.1
    rounds: int = 1
    floors_source: str = "default"  # calibrated | default | mixed
    env_fingerprint: str = ""       # host tool triple; additive, default empty

    def to_dict(self) -> dict:
        d = {
            "verdict": self.verdict,
            "bench_ir_rows": dict(self.bench_ir_rows),
            "profile_fingerprint": self.profile_fingerprint,
            "epsilon_pct": self.epsilon_pct,
            "rounds": int(self.rounds),
            "floors_source": self.floors_source,
            "notes": list(self.notes),
            "rows": [
                {"row_key": r.row_key, "base_ir": r.base_ir, "cand_ir": r.cand_ir,
                 "delta_pct": r.delta_pct, "status": r.status,
                 "floor_pct": r.floor_pct}
                for r in self.rows
            ],
        }
        if self.env_fingerprint:
            d["env_fingerprint"] = self.env_fingerprint
        return d


# --- config resolution -------------------------------------------------------

def has_terminal_config(spec) -> bool:
    """True when the target declares terminal_bench_targets (terminal gate on)."""
    return bool(spec_field(spec, "terminal_bench_targets", default=None))


def terminal_bench_targets(spec) -> list:
    t = spec_field(spec, "terminal_bench_targets", default=None)
    return list(t) if t else []


def terminal_bench_filter(spec) -> Optional[str]:
    f = spec_field(spec, "terminal_bench_filter", default=None)
    return str(f) if f else None


def resolve_measure_bin(spec=None) -> str:
    """Binary path for `mega-bench-reporter`. Env wins over target JSON.

    Raises TerminalError with a clear message when unset — never a traceback
    about NoneType paths.
    """
    env = os.environ.get("ARO_MEASURE_BIN")
    if env is not None and str(env).strip():
        return str(env).strip()
    mb = spec_field(spec, "measure_bin", default=None)
    if mb is not None and str(mb).strip():
        return str(mb).strip()
    raise TerminalError(
        "measure binary unset: set env ARO_MEASURE_BIN or target JSON field "
        "`measure_bin` (server-side path to mega-bench-reporter) before "
        "invoking the terminal gate")


def package_name(spec) -> str:
    """Criterion package: benchmark_probe.pkg (same crate the probe builds)."""
    bench = getattr(spec, "bench", None) or {}
    if isinstance(bench, dict) and bench.get("pkg"):
        return str(bench["pkg"])
    raw = getattr(spec, "raw", None) or {}
    bp = raw.get("benchmark_probe") or {}
    if bp.get("pkg"):
        return str(bp["pkg"])
    raise TerminalError("cannot resolve package name: set benchmark_probe.pkg")


def resolve_terminal_timeout(spec) -> float:
    """Seconds for one measure invocation.

    Default is 4× spec.timeout: measure = build + full criterion bench under
    valgrind. Override with target JSON field `terminal_timeout_secs`.
    """
    v = spec_field(spec, "terminal_timeout_secs", default=None, cast=float)
    if v is not None:
        return v
    base = spec_field(spec, "timeout", default=1800, cast=float)
    return 4.0 * float(base)


def resolve_terminal_rounds(spec=None) -> int:
    """How many times to measure each side. Env ARO_TERMINAL_ROUNDS wins."""
    env = os.environ.get("ARO_TERMINAL_ROUNDS")
    if env is not None and str(env).strip() != "":
        n = int(env)
        if n < 1:
            raise TerminalError("ARO_TERMINAL_ROUNDS must be >= 1")
        return n

    def _at_least_one(n):
        if n < 1:
            raise TerminalError("terminal_measure_rounds must be >= 1")

    return spec_field(
        spec, "terminal_measure_rounds", default=DEFAULT_TERMINAL_ROUNDS,
        cast=int, validate=_at_least_one)


def resolve_default_floor_pct(spec=None) -> float:
    """Conservative floor when a row has no calibrated entry. Default 1.0%."""
    return spec_field(
        spec, "terminal_default_floor_pct",
        default=DEFAULT_TERMINAL_FLOOR_PCT, cast=float)


def resolve_control_lanes(spec=None) -> list:
    """Upstream control-lane names excluded from subject improved/regressed.

    Empty list when absent → legacy single-threshold verdict on every row.
    """
    v = spec_field(spec, "control_lanes", default=None)
    if not v:
        return []
    return [str(x) for x in v]


def resolve_control_composition_bound_pct(spec=None) -> float:
    """|Δ%| bound for control rows. Default 2.0 when control_lanes is declared."""
    v = spec_field(spec, "control_composition_bound_pct", default=None,
                   cast=float)
    if v is not None:
        return v
    # Declared lanes without an explicit bound → default composition bound.
    if resolve_control_lanes(spec):
        return DEFAULT_CONTROL_COMPOSITION_BOUND_PCT
    return DEFAULT_CONTROL_COMPOSITION_BOUND_PCT


def resolve_protected_row_families(spec=None) -> list:
    """Row families (first `/`-segment) that cannot be traded away.

    Empty list when absent → legacy single-threshold verdict (no policy).
    """
    v = spec_field(spec, "protected_row_families", default=None)
    if not v:
        return []
    return [str(x) for x in v]


def resolve_tradeable_regression_cap_pct(spec=None) -> Optional[float]:
    """Max Δ% for a tradeable-family regression under WITH_TRADE. None when absent."""
    return spec_field(spec, "tradeable_regression_cap_pct", default=None,
                      cast=float)


def resolve_protected_hysteresis(spec=None) -> Optional[dict]:
    """Hysteresis knobs for protected-family regressions. None when absent.

    Shape: ``{"margin_pp": float, "floor_multiple": float}``.
    """
    v = spec_field(spec, "protected_hysteresis", default=None)
    if not v or not isinstance(v, dict):
        return None
    out = {}
    if "margin_pp" in v:
        out["margin_pp"] = float(v["margin_pp"])
    if "floor_multiple" in v:
        out["floor_multiple"] = float(v["floor_multiple"])
    return out or None


def has_row_family_policy(spec=None) -> bool:
    """True when the spec declares protected_row_families (policy gate on)."""
    return bool(resolve_protected_row_families(spec))


def row_family(row_key: str) -> str:
    """First `/`-separated segment of a criterion row key (empty if blank)."""
    s = str(row_key or "")
    if not s:
        return ""
    return s.split("/", 1)[0]


def hysteresis_ceiling(floor_pct: float, hysteresis: Optional[dict]) -> float:
    """H = max(floor + margin_pp, floor_multiple × floor). Defaults 0.05 / 1.5."""
    fl = float(floor_pct)
    h = hysteresis or {}
    margin = float(h.get("margin_pp", 0.05))
    mult = float(h.get("floor_multiple", 1.5))
    return max(fl + margin, mult * fl)


def is_control_row(row_key: str, control_lanes) -> bool:
    """True when any `/`-separated path segment exactly equals a control lane.

    Segment-exact match (not substring): `log_opcodes/op_revm_latest/log4_32b`
    is control when `op_revm_latest` is listed; `revm_pinned_x/rex5/case` is not
    control for lane `revm_pinned` (suffix/prefix tokens do not match).
    Robust to nesting differences because every path segment is checked.
    """
    if not control_lanes:
        return False
    lanes = set(control_lanes)
    return any(seg in lanes for seg in str(row_key).split("/"))


def resolve_test_full(spec) -> Optional[list]:
    """Optional full-suite correctness command at the terminal gate.

    Reads `correctness_oracle.test_full` from the authored target JSON
    (`spec.raw`). Absent / empty → None (legacy behaviour: no suite run).
    Inner-loop `correctness_oracle.test` (--lib) is untouched.
    Nested under correctness_oracle — not a top-level spec_field.
    """
    if spec is None:
        return None
    raw = getattr(spec, "raw", None) or {}
    oracle = raw.get("correctness_oracle") or {}
    cmd = oracle.get("test_full")
    if not cmd:
        return None
    if not isinstance(cmd, list):
        raise TerminalError(
            "correctness_oracle.test_full must be a command token list, "
            f"got {type(cmd).__name__}")
    return [str(x) for x in cmd]


def resolve_test_full_timeout(spec) -> float:
    """Seconds for the optional terminal-gate full correctness suite.

    Override with target JSON field `test_full_timeout_secs`. Default 1800 —
    independent of `terminal_timeout_secs` (which budgets measure under valgrind).
    """
    return float(spec_field(
        spec, "test_full_timeout_secs",
        default=float(DEFAULT_TEST_FULL_TIMEOUT_SECS), cast=float))


def _default_test_full_runner(cmd: list, *, cwd, timeout: Optional[float] = None
                              ) -> tuple:
    """(stdout, stderr, returncode) for test_full. Injectable for tests."""
    p = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr, p.returncode


def run_test_full(cmd: list, candidate_dir, *,
                  timeout: Optional[float] = None,
                  runner: Optional[Callable] = None) -> tuple:
    """Run correctness_oracle.test_full in the candidate checkout only.

    Returns (stdout, stderr, returncode). Does not run on baseline_dir — the
    baseline is the frozen reference; its suite already passed when it became
    baseline.
    """
    run = runner or _default_test_full_runner
    return run(list(cmd), cwd=candidate_dir, timeout=timeout)


def _test_full_failed_result(rc: int, stdout: str, stderr: str, *,
                             env_fp: str = "") -> TerminalResult:
    """Build a TERMINAL_TEST_FAILED result carrying the last ~2k of test output."""
    combined = ((stdout or "") + "\n" + (stderr or "")).strip()
    tail = combined[-_TEST_FULL_OUTPUT_TAIL:] if combined else f"(no output; exit {rc})"
    notes = [
        f"verdict: {TERMINAL_TEST_FAILED} — correctness_oracle.test_full "
        f"failed (exit {rc}) in candidate checkout; no measurement performed",
        tail,
    ]
    result = TerminalResult(
        verdict=TERMINAL_TEST_FAILED,
        bench_ir_rows={},
        profile_fingerprint="",
        rows=[],
        notes=notes,
        rounds=0,
        floors_source="n/a",
    )
    if env_fp:
        result.env_fingerprint = env_fp
    return result


# --- floors file (memory/floors/<spec>.json; versioned) ----------------------

def floors_dir() -> Path:
    env = os.environ.get("ARO_FLOORS_DIR")
    if env is not None and str(env).strip():
        return Path(str(env).strip())
    return REPO_ROOT / "memory" / "floors"


def floors_path(spec_name: str) -> Path:
    return floors_dir() / f"{spec_name}.json"


def rustc_version() -> str:
    """Current host `rustc -V` (empty string when rustc is unavailable)."""
    try:
        p = subprocess.run(
            ["rustc", "-V"], capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            return (p.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def checkout_describe(checkout) -> str:
    """`git describe --always --dirty` of the measured checkout (best-effort)."""
    try:
        p = subprocess.run(
            ["git", "-C", str(checkout), "describe", "--always", "--dirty"],
            capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            return (p.stdout or "").strip()
        p = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            return (p.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def measure_bin_label(measure_bin: str) -> str:
    """Path, or first line of `--version` when the binary supports it."""
    try:
        p = subprocess.run(
            [str(measure_bin), "--version"],
            capture_output=True, text=True, timeout=10)
        if p.returncode == 0 and (p.stdout or "").strip():
            return (p.stdout or "").strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return str(measure_bin)


def pairwise_abs_pct(a: int, b: int) -> float:
    """Symmetric pairwise |Δ%| = max(|a-b|/a, |a-b|/b) * 100."""
    if a == 0 and b == 0:
        return 0.0
    if a == 0 or b == 0:
        raise TerminalError(
            f"instr_count is 0 in A/A pair ({a}, {b}) — measurement unusable")
    return max(abs(a - b) / a, abs(a - b) / b) * 100.0


def max_pairwise_delta_pct(values) -> float:
    """Max pairwise |Δ%| across a sequence of instr counts for one row."""
    vals = [int(v) for v in values]
    if len(vals) < 2:
        return 0.0
    best = 0.0
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            dp = pairwise_abs_pct(vals[i], vals[j])
            if dp > best:
                best = dp
    return best


def calibrate_row_floor(values, *, min_floor_pct: float,
                        safety: float = FLOOR_SAFETY_FACTOR) -> float:
    """floor_pct = max(max_pairwise|Δ%| × safety, min_floor_pct)."""
    return max(max_pairwise_delta_pct(values) * float(safety), float(min_floor_pct))


def compute_floors_from_docs(docs: list, *, min_floor_pct: float,
                             safety: float = FLOOR_SAFETY_FACTOR) -> dict:
    """Per-row floors from N measure docs of the same checkout.

    All docs must share the same row-key set (caller enforces fingerprint /
    row-set consistency before calling, or this raises TerminalError).
    """
    if not docs:
        raise TerminalError("calibrate: no measure docs")
    keys = set(docs[0].rows)
    for d in docs[1:]:
        if set(d.rows) != keys:
            raise TerminalError(
                f"calibrate row-set mismatch across rounds: "
                f"first={sorted(keys)} other={sorted(d.rows)}")
    floors: dict = {}
    for k in sorted(keys):
        vals = [int(d.rows[k]) for d in docs]
        floors[k] = calibrate_row_floor(vals, min_floor_pct=min_floor_pct,
                                        safety=safety)
    return floors


def write_floors(spec_name: str, floors: dict, *, meta: dict,
                 path: Optional[Path] = None) -> Path:
    """Write memory/floors/<spec>.json (committed institutional memory)."""
    dest = path if path is not None else floors_path(spec_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": dict(meta), "floors": {str(k): float(v) for k, v in floors.items()}}
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return dest


def load_floors(spec_name: str, *, path: Optional[Path] = None
                ) -> tuple:
    """Load floors file.

    Returns (floors_map, meta, warnings). Missing file → ({}, {}, [warn]).
    Staleness (rustc mismatch, calibrated_at older than 30d) is warning-only.
    """
    dest = path if path is not None else floors_path(spec_name)
    if not dest.is_file():
        return {}, {}, [
            f"terminal floors: no calibrated file at {dest} — using "
            f"default floor for every row (run `aro terminal-calibrate`)"
        ]
    try:
        doc = json.loads(dest.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {}, {}, [f"terminal floors: failed to read {dest}: {e} — using defaults"]
    if not isinstance(doc, dict):
        return {}, {}, [f"terminal floors: {dest} root is not an object — using defaults"]
    raw_floors = doc.get("floors") or {}
    if not isinstance(raw_floors, dict):
        return {}, {}, [f"terminal floors: {dest} 'floors' is not an object — using defaults"]
    floors: dict = {}
    warnings: list = []
    for k, v in raw_floors.items():
        # Skip-on-unparseable (same path for non-finite / non-positive below):
        # omit the key so the gate falls back to default_floor_pct.
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        # NaN/inf make both Δ comparisons False (silent UNTOUCHED corruption);
        # non-positive floors invert or zero out classification. Reject both.
        if not math.isfinite(fv) or fv <= 0:
            warnings.append(
                f"terminal floors: skipping row {k!r} with invalid floor {v!r} "
                f"(must be finite and > 0) — using default"
            )
            continue
        floors[str(k)] = fv
    meta = doc.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    # rustc mismatch (warn, not error)
    cur = rustc_version()
    cal_rustc = str(meta.get("rustc") or "")
    if cur and cal_rustc and cur != cal_rustc:
        warnings.append(
            f"terminal floors: rustc mismatch (calibrated={cal_rustc!r} "
            f"current={cur!r}) — re-run terminal-calibrate after tool upgrades")
    # age > 30 days
    cal_at = str(meta.get("calibrated_at") or "")
    if cal_at:
        try:
            # Accept trailing Z.
            ts = cal_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
            if age.days > FLOORS_STALE_DAYS:
                warnings.append(
                    f"terminal floors: calibrated_at {cal_at} is {age.days}d old "
                    f"(>{FLOORS_STALE_DAYS}d) — re-run terminal-calibrate periodically")
        except ValueError:
            warnings.append(
                f"terminal floors: calibrated_at {cal_at!r} is not ISO — ignoring age check")
    return floors, meta, warnings


def floors_source_for(row_keys, floors: dict) -> str:
    """calibrated / default / mixed based on which keys have file entries."""
    if not floors:
        return "default"
    hits = sum(1 for k in row_keys if k in floors)
    if hits == 0:
        return "default"
    if hits == len(row_keys):
        return "calibrated"
    return "mixed"


# --- measure CLI I/O ---------------------------------------------------------

def parse_measure_stdout(text: str) -> MeasureDoc:
    """Parse the single JSON document measure prints on stdout.

    Expected shape:
      {"rows": {"<row_key>": {"instr_count": <u64>}, ...},
       "meta": {"rustc": "...", "profile_fingerprint": "..."}}
    Row values may also carry `ns` under --walltime — ignored here.
    """
    text = (text or "").strip()
    if not text:
        raise TerminalError("measure produced empty stdout (expected one JSON document)")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise TerminalError(f"measure stdout is not JSON: {e}") from e
    if not isinstance(doc, dict):
        raise TerminalError(f"measure JSON root must be an object, got {type(doc).__name__}")

    raw_rows = doc.get("rows") or {}
    if not isinstance(raw_rows, dict):
        raise TerminalError("measure JSON 'rows' must be an object")
    rows: dict = {}
    for k, v in raw_rows.items():
        if isinstance(v, dict):
            if "instr_count" not in v:
                raise TerminalError(
                    f"measure row {k!r} missing instr_count "
                    f"(did you forget --instructions?)")
            try:
                rows[str(k)] = int(v["instr_count"])
            except (TypeError, ValueError) as e:
                raise TerminalError(
                    f"measure row {k!r} instr_count not an integer: "
                    f"{v['instr_count']!r}") from e
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            rows[str(k)] = int(v)
        else:
            raise TerminalError(
                f"measure row {k!r} has unexpected value type {type(v).__name__}")

    meta = doc.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    # Hard-error on absent/empty fingerprint — never default to '' (two
    # malformed responses would otherwise pass the drift check as ''=='').
    fp_raw = meta.get("profile_fingerprint")
    if fp_raw is None or str(fp_raw).strip() == "":
        raise TerminalError(
            "measure meta.profile_fingerprint missing or empty "
            "(required for config-drift check)")
    fp = str(fp_raw)
    rustc = str(meta.get("rustc") or "")
    return MeasureDoc(rows=rows, meta=meta, profile_fingerprint=fp, rustc=rustc)


def build_measure_cmd(measure_bin: str, checkout, *, package: str,
                      bench_targets: list, bench_filter: Optional[str] = None) -> list:
    cmd = [str(measure_bin), "measure",
           "--checkout", str(checkout),
           "--package", str(package),
           "--instructions"]
    for t in bench_targets:
        cmd.extend(["--bench-target", str(t)])
    if bench_filter:
        cmd.extend(["--bench-filter", str(bench_filter)])
    return cmd


def _default_runner(cmd: list, timeout: Optional[float] = None) -> tuple:
    """(stdout, stderr, returncode) via subprocess. Injectable for tests.

    `timeout` is seconds for one measure invocation (build + full criterion
    bench under valgrind). None means no subprocess timeout.
    """
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr, p.returncode


def measure_checkout(checkout, *, package: str, bench_targets: list,
                     measure_bin: str, bench_filter: Optional[str] = None,
                     timeout: Optional[float] = None,
                     runner: Optional[Callable] = None) -> MeasureDoc:
    """Invoke measure on one worktree and parse its JSON stdout."""
    if not bench_targets:
        raise TerminalError("terminal_bench_targets is empty — nothing to measure")
    cmd = build_measure_cmd(measure_bin, checkout, package=package,
                            bench_targets=bench_targets, bench_filter=bench_filter)
    run = runner or _default_runner
    try:
        stdout, stderr, rc = run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Same pattern as target.icount valgrind-timeout handling.
        raise TerminalError(
            f"measure timed out after {timeout}s "
            f"(build + full criterion bench under valgrind; "
            f"override via target JSON field terminal_timeout_secs)")
    if rc != 0:
        msg = (stderr or stdout or "").strip() or f"exit {rc}"
        raise TerminalError(f"measure failed (exit {rc}): {msg}")
    return parse_measure_stdout(stdout)


def median_ir(values) -> int:
    """Median of integer Ir samples; rounds half away from zero for even N."""
    vals = [int(v) for v in values]
    if not vals:
        raise TerminalError("median_ir: empty sample list")
    m = _median(vals)
    if m != m:  # NaN
        raise TerminalError("median_ir: no finite samples")
    return int(round(m))


def median_measure_docs(docs: list) -> MeasureDoc:
    """Collapse N measure docs into one via per-row median Ir.

    Hard-errors on fingerprint or row-set drift across rounds of the same side
    (same shape as baseline-vs-candidate checks).
    """
    if not docs:
        raise TerminalError("median_measure_docs: no docs")
    if len(docs) == 1:
        return docs[0]
    fp = docs[0].profile_fingerprint
    keys = set(docs[0].rows)
    for i, d in enumerate(docs[1:], start=1):
        if d.profile_fingerprint != fp:
            raise TerminalError(
                f"config drift across measure rounds of the same side: "
                f"round0 fp={fp!r} round{i} fp={d.profile_fingerprint!r}")
        if set(d.rows) != keys:
            only0 = sorted(keys - set(d.rows))
            onlyi = sorted(set(d.rows) - keys)
            raise TerminalError(
                f"row-set mismatch across measure rounds of the same side: "
                f"dropped={only0} new={onlyi}")
    med_rows = {k: median_ir(d.rows[k] for d in docs) for k in keys}
    return MeasureDoc(
        rows=med_rows,
        meta=dict(docs[0].meta),
        profile_fingerprint=fp,
        rustc=docs[0].rustc,
    )


def measure_checkout_rounds(checkout, *, package: str, bench_targets: list,
                            measure_bin: str, rounds: int,
                            bench_filter: Optional[str] = None,
                            timeout: Optional[float] = None,
                            runner: Optional[Callable] = None) -> MeasureDoc:
    """Measure one checkout `rounds` times; return the per-row median doc."""
    n = int(rounds)
    if n < 1:
        raise TerminalError("rounds must be >= 1")
    docs = [
        measure_checkout(checkout, package=package, bench_targets=bench_targets,
                         measure_bin=measure_bin, bench_filter=bench_filter,
                         timeout=timeout, runner=runner)
        for _ in range(n)
    ]
    return median_measure_docs(docs)


# --- adjudication ------------------------------------------------------------

def _row_delta_pct(base_ir: int, cand_ir: int) -> float:
    if base_ir == 0:
        if cand_ir == 0:
            return 0.0
        raise TerminalError(
            f"baseline instr_count is 0 with candidate={cand_ir} — measurement unusable")
    return (cand_ir - base_ir) / base_ir * 100.0


def _row_field(row, name: str):
    """Read a field from a RowDelta or a row dict."""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def verdict_from_rows(rows) -> tuple:
    """Pure aggregation over row statuses → (verdict, improved, regressed, control_exceeded).

    Consumes RowDelta-shaped objects or dicts (must expose `status`). Control
    rows (`control-ok` / `control-anomaly`) never count as improved/regressed;
    any `control-anomaly` forces TERMINAL_CONTROL_ANOMALY (fail-closed).

    Legacy / floor-only aggregation — no row-family policy. Callers that need
    policy-aware outcomes use ``policy_aware_verdict``.
    """
    improved = regressed = control_exceeded = 0
    for r in rows or []:
        st = str(_row_field(r, "status") or "")
        if st == "improved":
            improved += 1
        elif st == "regressed":
            regressed += 1
        elif st == "control-anomaly":
            control_exceeded += 1
    if control_exceeded:
        verdict = TERMINAL_CONTROL_ANOMALY
    elif improved and not regressed:
        verdict = TERMINAL_CONFIRMED
    elif not improved and not regressed:
        verdict = TERMINAL_UNTOUCHED
    elif regressed and not improved:
        verdict = TERMINAL_REGRESSED
    else:
        verdict = TERMINAL_MIXED
    return verdict, improved, regressed, control_exceeded


def classify_subject_regression(row, *, protected_families, cap_pct,
                                hysteresis) -> str:
    """Classify one subject-row regression under row-family policy.

    Returns ``\"band\"`` | ``\"traded\"`` | ``\"violation\"``. Control rows must
    not be passed here. ``row`` is RowDelta or dict with row_key/delta_pct/floor_pct.
    """
    key = str(_row_field(row, "row_key") or "")
    dp = float(_row_field(row, "delta_pct") or 0.0)
    fl = float(_row_field(row, "floor_pct") or 0.0)
    fam = row_family(key)
    protected = set(protected_families or [])
    if fam in protected:
        h = hysteresis_ceiling(fl, hysteresis)
        # Δ ≤ floor is never status=regressed; band is floor < Δ ≤ H.
        if dp <= h:
            return "band"
        return "violation"
    cap = float(cap_pct) if cap_pct is not None else 0.0
    if dp <= cap:
        return "traded"
    return "violation"


def policy_aware_verdict(rows, *, protected_row_families=None,
                         tradeable_regression_cap_pct=None,
                         protected_hysteresis=None) -> tuple:
    """Floor aggregation + optional row-family policy → (verdict, imp, reg, ce, notes).

    When ``protected_row_families`` is empty/None, returns the same verdict as
    ``verdict_from_rows`` with empty extra notes (byte-identical legacy path).

    Policy (only when families are declared):
      - control anomalies still force TERMINAL_CONTROL_ANOMALY
      - subject regressions in protected families: band (≤ H) or violation (> H)
      - subject regressions in tradeable families: traded (≤ cap) or violation
      - band does not block CONFIRMED / WITH_TRADE
      - all-traded-within-cap + ≥1 improvement → TERMINAL_CONFIRMED_WITH_TRADE
      - any violation → MIXED/REGRESSED exactly as floor-based logic would for
        remaining blocking regressions
    """
    legacy, improved, regressed, control_exceeded = verdict_from_rows(rows)
    families = [str(x) for x in (protected_row_families or [])]
    if not families:
        return legacy, improved, regressed, control_exceeded, []

    extra_notes: list = []
    if control_exceeded:
        return (TERMINAL_CONTROL_ANOMALY, improved, regressed, control_exceeded,
                extra_notes)

    traded: list = []
    bands: list = []
    violations: list = []
    cap = tradeable_regression_cap_pct
    if cap is None:
        cap = 0.0
    for r in rows or []:
        st = str(_row_field(r, "status") or "")
        if st != "regressed":
            continue
        kind = classify_subject_regression(
            r, protected_families=families, cap_pct=cap,
            hysteresis=protected_hysteresis)
        key = str(_row_field(r, "row_key") or "")
        dp = float(_row_field(r, "delta_pct") or 0.0)
        fl = float(_row_field(r, "floor_pct") or 0.0)
        if kind == "band":
            h = hysteresis_ceiling(fl, protected_hysteresis)
            bands.append(r)
            extra_notes.append(
                f"band: {key} {dp:+.4f}% (floor {fl}% H {h:.4f}%)")
        elif kind == "traded":
            traded.append(r)
            extra_notes.append(
                f"traded: {key} {dp:+.4f}% (cap {float(cap)}%)")
        else:
            violations.append(r)

    blocking = len(violations)
    n_traded = len(traded)
    if improved and not blocking and not n_traded:
        verdict = TERMINAL_CONFIRMED
    elif improved and not blocking and n_traded:
        verdict = TERMINAL_CONFIRMED_WITH_TRADE
    elif not improved and not blocking and not n_traded:
        # Only bands (or no subject regressions) remain → untouched.
        verdict = TERMINAL_UNTOUCHED
    elif blocking and not improved:
        verdict = TERMINAL_REGRESSED
    elif n_traded and not improved and not blocking:
        # Tradeable regressions without a subject win are still a regression.
        verdict = TERMINAL_REGRESSED
    else:
        # improved+blocking, or any mixed residual.
        verdict = TERMINAL_MIXED if improved else TERMINAL_REGRESSED
        if improved and blocking:
            verdict = TERMINAL_MIXED
    return verdict, improved, regressed, control_exceeded, extra_notes


def _expected_row_status(delta_pct: float, floor_pct: float,
                         stored_status: str) -> str:
    """Re-derive status from Δ vs floor. Control rows use floor as composition bound."""
    if str(stored_status or "").startswith("control-"):
        if abs(delta_pct) > floor_pct:
            return "control-anomaly"
        return "control-ok"
    if delta_pct < -floor_pct:
        return "improved"
    if delta_pct > floor_pct:
        return "regressed"
    return "untouched"


def verify_terminal_doc(doc: dict, *,
                        control_lanes=None,
                        control_bound_pct=None,
                        protected_row_families=None,
                        tradeable_regression_cap_pct=None,
                        protected_hysteresis=None) -> None:
    """Recompute every row delta/status and the verdict; hard-error on mismatch.

    Tamper alarm — not a verdict. Every consumer that loads terminal.json must
    call this before trusting stored `verdict` / row fields. Hand-edited
    plaintext verdicts that disagree with the rows are rejected here.

    Lane-less mode (`control_lanes is None`): self-consistency only — control
    class is taken from the stored status prefix. Sufficient as a tamper alarm
    for delta/verdict edits, NOT sufficient for mergeability (a subject row
    relabelled `control-ok` with a raised floor still self-consistently
    verifies). Mergeable-unlocking ingestion must pass `control_lanes` (use
    `[]` when the spec declares none) so class is re-derived from `row_key`
    via `is_control_row`. Derived-control rows also require
    `floor_pct == control_bound_pct` when a bound is provided (tol 1e-9).

    Row-family policy kwargs must match the judging spec when the stored
    verdict is WITH_TRADE (absent policy → WITH_TRADE cannot recompute).
    """
    if not isinstance(doc, dict):
        raise TerminalError("verify: terminal doc must be a JSON object")
    stored_verdict = doc.get("verdict")
    row_list = doc.get("rows") or []
    if not isinstance(row_list, list):
        raise TerminalError("verify: terminal doc 'rows' must be a list")

    # No-measurement outcomes: empty rows, verdict is not a function of Δ.
    if stored_verdict == TERMINAL_TEST_FAILED:
        if row_list:
            raise TerminalError(
                "verify: TERMINAL_TEST_FAILED must have empty rows[] "
                f"(got {len(row_list)})")
        return

    # None → lane-less self-consistency; a list (even empty) → lane-aware.
    lane_aware = control_lanes is not None
    lanes = [str(x) for x in control_lanes] if lane_aware else None
    bound_f: Optional[float] = None
    if control_bound_pct is not None:
        bound_f = float(control_bound_pct)

    for i, r in enumerate(row_list):
        if not isinstance(r, dict):
            raise TerminalError(f"verify: rows[{i}] is not an object")
        key = r.get("row_key")
        label = repr(key) if key else f"rows[{i}]"
        if "base_ir" not in r or "cand_ir" not in r:
            raise TerminalError(f"verify: row {label} missing base_ir/cand_ir")
        try:
            base_ir = int(r["base_ir"])
            cand_ir = int(r["cand_ir"])
        except (TypeError, ValueError) as e:
            raise TerminalError(
                f"verify: row {label} base_ir/cand_ir not integers") from e
        try:
            recomputed_dp = _row_delta_pct(base_ir, cand_ir)
        except TerminalError as e:
            raise TerminalError(f"verify: row {label}: {e}") from e
        stored_dp = r.get("delta_pct")
        if stored_dp is None:
            raise TerminalError(f"verify: row {label} missing delta_pct")
        try:
            stored_dp_f = float(stored_dp)
        except (TypeError, ValueError) as e:
            raise TerminalError(
                f"verify: row {label} delta_pct not a number") from e
        if abs(stored_dp_f - recomputed_dp) > 0.001:
            raise TerminalError(
                f"verify: row {label} delta_pct mismatch "
                f"(stored={stored_dp_f} recomputed={recomputed_dp})")

        floor = r.get("floor_pct")
        if floor is None:
            raise TerminalError(f"verify: row {label} missing floor_pct")
        try:
            floor_f = float(floor)
        except (TypeError, ValueError) as e:
            raise TerminalError(
                f"verify: row {label} floor_pct not a number") from e
        stored_status = r.get("status")
        if stored_status is None:
            raise TerminalError(f"verify: row {label} missing status")
        stored_status_s = str(stored_status)
        stored_control = stored_status_s.startswith("control-")

        if lane_aware:
            derived_control = is_control_row(str(key or ""), lanes)
            if derived_control != stored_control:
                raise TerminalError(
                    f"verify: row {label} control-class mismatch "
                    f"(stored_status={stored_status_s!r} "
                    f"derived_control={derived_control})")
            if derived_control and bound_f is not None:
                if abs(floor_f - bound_f) > 1e-9:
                    raise TerminalError(
                        f"verify: row {label} control floor_pct mismatch "
                        f"(stored={floor_f} bound={bound_f})")

        expected_status = _expected_row_status(
            recomputed_dp, floor_f, stored_status_s)
        if stored_status_s != expected_status:
            raise TerminalError(
                f"verify: row {label} status mismatch "
                f"(stored={stored_status_s!r} recomputed={expected_status!r})")

    recomputed_verdict, _imp, _reg, _ce, _notes = policy_aware_verdict(
        row_list,
        protected_row_families=protected_row_families,
        tradeable_regression_cap_pct=tradeable_regression_cap_pct,
        protected_hysteresis=protected_hysteresis,
    )
    if stored_verdict != recomputed_verdict:
        raise TerminalError(
            f"verify: verdict mismatch "
            f"(stored={stored_verdict!r} recomputed={recomputed_verdict!r})")


def judge_terminal(base: MeasureDoc, cand: MeasureDoc, *,
                   epsilon_pct: float,
                   floors: Optional[dict] = None,
                   default_floor_pct: Optional[float] = None,
                   floors_source: str = "default",
                   rounds: int = 1,
                   control_lanes: Optional[list] = None,
                   control_composition_bound_pct: Optional[float] = None,
                   protected_row_families: Optional[list] = None,
                   tradeable_regression_cap_pct: Optional[float] = None,
                   protected_hysteresis: Optional[dict] = None,
                   ) -> TerminalResult:
    """Diff two measure docs into a TERMINAL_* verdict.

    Per-row threshold is floor(row): calibrated value when present, else
    `default_floor_pct`. When `default_floor_pct` is None, falls back to
    `epsilon_pct` so callers that pass only ε keep the legacy single-threshold
    behaviour (floors all = ε).

    Lane-aware control rows: when `control_lanes` is non-empty, any row whose
    `/`-separated path segments include an exact control-lane name is classified
    as `control-ok` / `control-anomaly` against `control_composition_bound_pct`
    (default 2.0) and is NOT counted into improved/regressed. Any
    `control-anomaly` forces `TERMINAL_CONTROL_ANOMALY` (fail-closed). Absent
    `control_lanes` → byte-identical legacy behaviour on the same inputs.

    Row-family policy (when `protected_row_families` is non-empty): subject
    regressions in tradeable families with Δ ≤ cap plus ≥1 improvement yield
    `TERMINAL_CONFIRMED_WITH_TRADE`; protected regressions past hysteresis are
    violations (MIXED/REGRESSED); band-zone protected regressions do not block
    CONFIRMED/WITH_TRADE. Absent policy fields → byte-identical legacy path.

    Hard errors (not verdicts):
      - profile_fingerprint mismatch → config drift
      - row keys present on one side only → bench-set anomaly
    """
    if base.profile_fingerprint != cand.profile_fingerprint:
        raise TerminalError(
            f"config drift: profile_fingerprint mismatch "
            f"baseline={base.profile_fingerprint!r} "
            f"candidate={cand.profile_fingerprint!r} "
            f"(never a verdict — fix the worktree profiles / rustc pin)")

    base_keys = set(base.rows)
    cand_keys = set(cand.rows)
    only_base = sorted(base_keys - cand_keys)
    only_cand = sorted(cand_keys - base_keys)
    if only_base or only_cand:
        raise TerminalError(
            f"row-set mismatch (bench set must match across sides): "
            f"dropped={only_base} new={only_cand}")

    floor_map = dict(floors or {})
    if default_floor_pct is None:
        default_floor_pct = float(epsilon_pct)
    else:
        default_floor_pct = float(default_floor_pct)

    # Source label: if caller didn't already compute it from the file, derive.
    src = floors_source
    if floors is not None and floors_source == "default":
        # Only re-derive when caller left the default label — run_terminal sets
        # it explicitly, so this mainly helps direct unit-test callers.
        src = floors_source_for(base_keys, floor_map)

    lanes = [str(x) for x in (control_lanes or [])]
    bound: Optional[float] = None
    if lanes:
        if control_composition_bound_pct is None:
            bound = float(DEFAULT_CONTROL_COMPOSITION_BOUND_PCT)
        else:
            bound = float(control_composition_bound_pct)

    rows: list = []
    control_abs_deltas: list = []
    nonzero: dict = {}
    for k in sorted(base_keys):
        b, c = int(base.rows[k]), int(cand.rows[k])
        dp = _row_delta_pct(b, c)
        if lanes and is_control_row(k, lanes):
            # Control rows use the composition bound, not the noise floor.
            assert bound is not None
            fl = bound
            control_abs_deltas.append(abs(dp))
            if abs(dp) > bound:
                status = "control-anomaly"
            else:
                status = "control-ok"
            rows.append(RowDelta(k, b, c, dp, status, floor_pct=fl))
            if b != c:
                nonzero[k] = round(dp, 4)
            continue
        fl = float(floor_map[k]) if k in floor_map else default_floor_pct
        if dp < -fl:
            status = "improved"
        elif dp > fl:
            status = "regressed"
        else:
            status = "untouched"
        rows.append(RowDelta(k, b, c, dp, status, floor_pct=fl))
        if b != c:
            nonzero[k] = round(dp, 4)

    # Cap the summary map (stable order: largest |Δ| first).
    if len(nonzero) > _MAX_BENCH_IR_ROWS:
        top = sorted(nonzero.items(), key=lambda kv: abs(kv[1]), reverse=True)
        nonzero = dict(top[:_MAX_BENCH_IR_ROWS])

    verdict, improved, regressed, control_anom, policy_notes = policy_aware_verdict(
        rows,
        protected_row_families=protected_row_families,
        tradeable_regression_cap_pct=tradeable_regression_cap_pct,
        protected_hysteresis=protected_hysteresis,
    )

    n_control = len(control_abs_deltas)
    notes = [
        f"terminal gate: rows={len(rows)} improved={improved} "
        f"regressed={regressed} floors_source={src} rounds={int(rounds)} "
        f"fp={base.profile_fingerprint!r}",
    ]
    if lanes:
        max_abs = max(control_abs_deltas) if control_abs_deltas else 0.0
        med_abs = float(_median(control_abs_deltas)) if control_abs_deltas else 0.0
        notes.append(
            f"control rows: n={n_control} max|Δ%|={max_abs:.4f} "
            f"median|Δ%|={med_abs:.4f} bound={bound}% exceeded={control_anom}"
        )
    notes.extend(policy_notes)

    if control_anom:
        # Fail-closed: measurement itself is suspect when a control lane moves
        # beyond the composition bound — regardless of subject-row outcomes.
        notes.append(
            f"verdict: {TERMINAL_CONTROL_ANOMALY} — {control_anom} control "
            f"row(s) |Δ%| > composition bound {bound}% (measurement suspect)")
    elif verdict == TERMINAL_CONFIRMED:
        notes.append("verdict: TERMINAL_CONFIRMED — ≥1 criterion row improved, none regressed")
    elif verdict == TERMINAL_CONFIRMED_WITH_TRADE:
        notes.append(
            "verdict: TERMINAL_CONFIRMED_WITH_TRADE — ≥1 criterion row improved; "
            "subject regressions are tradeable within cap (net win)")
    elif verdict == TERMINAL_UNTOUCHED:
        notes.append(
            "verdict: TERMINAL_UNTOUCHED — every criterion row |ΔIr| ≤ floor "
            "(probe-vs-bench divergence; block PR — #326/#332 shape)")
    elif verdict == TERMINAL_REGRESSED:
        notes.append("verdict: TERMINAL_REGRESSED — ≥1 criterion row worse beyond floor")
    else:
        notes.append(
            "verdict: TERMINAL_MIXED — improvements AND regressions; "
            "ablate → prune → re-terminal (see decision table)")

    return TerminalResult(
        verdict=verdict,
        bench_ir_rows=nonzero,
        profile_fingerprint=base.profile_fingerprint,
        rows=rows,
        notes=notes,
        epsilon_pct=float(epsilon_pct),
        rounds=int(rounds),
        floors_source=src,
    )


def run_terminal(spec, baseline_dir, candidate_dir, *,
                 runner: Optional[Callable] = None,
                 measure_bin: Optional[str] = None,
                 timeout: Optional[float] = None,
                 rounds: Optional[int] = None,
                 floors: Optional[dict] = None,
                 floors_path_override: Optional[Path] = None,
                 skip_selfcheck: bool = False,
                 version_runner: Optional[Callable] = None,
                 test_full_runner: Optional[Callable] = None) -> TerminalResult:
    """Measure both worktrees (median-of-N) and adjudicate with per-row floors.

    Pure of lessons/permtree I/O. Floors file missing → default floor for every
    row + one stderr warning. Requires a valid selfcheck marker unless
    `ARO_SKIP_SELFCHECK=1` or `skip_selfcheck=True` (hermetic tests).
    `version_runner` injects tool-version probing for hermetic tests.
    `test_full_runner` injects the optional full-suite correctness subprocess
    (hermetic tests; production uses cargo via `_default_test_full_runner`).

    When the spec declares `correctness_oracle.test_full`, that suite runs once
    in **candidate_dir only** before any measurement. Fail-fast: non-zero exit
    yields TERMINAL_TEST_FAILED and skips both measure rounds. Baseline is not
    re-tested — it is the frozen reference whose suite already passed when it
    became baseline.
    """
    if not has_terminal_config(spec):
        raise TerminalError(
            "spec has no terminal_bench_targets — terminal gate not configured "
            "(add the field to the target JSON or skip the gate)")

    env_fp = ""
    from . import selfcheck as scmod
    try:
        env_fp = scmod.require_selfcheck(
            spec, runner=version_runner, skip=skip_selfcheck) or ""
    except scmod.SelfcheckError as e:
        raise TerminalError(str(e)) from e

    # Optional full-suite correctness tier (fail fast, before 2×N measure rounds).
    # Do NOT run on baseline_dir: baseline is the frozen reference; its suite
    # already passed when it became baseline.
    test_full_cmd = resolve_test_full(spec)
    if test_full_cmd is not None:
        tf_to = resolve_test_full_timeout(spec)
        try:
            stdout, stderr, rc = run_test_full(
                test_full_cmd, candidate_dir, timeout=tf_to,
                runner=test_full_runner)
        except subprocess.TimeoutExpired:
            raise TerminalError(
                f"correctness_oracle.test_full timed out after {tf_to}s "
                f"in candidate checkout (override via test_full_timeout_secs)")
        if rc != 0:
            return _test_full_failed_result(rc, stdout, stderr, env_fp=env_fp)

    bin_path = measure_bin if measure_bin is not None else resolve_measure_bin(spec)
    targets = terminal_bench_targets(spec)
    filt = terminal_bench_filter(spec)
    pkg = package_name(spec)
    eps = ir_epsilon_pct(spec)
    to = timeout if timeout is not None else resolve_terminal_timeout(spec)
    n_rounds = int(rounds) if rounds is not None else resolve_terminal_rounds(spec)
    default_fl = resolve_default_floor_pct(spec)

    # Load calibrated floors (or empty + warnings).
    floor_warnings: list = []
    if floors is not None:
        floor_map = dict(floors)
    else:
        name = getattr(spec, "name", None) or "unknown"
        floor_map, _meta, floor_warnings = load_floors(
            str(name), path=floors_path_override)

    for w in floor_warnings:
        print(w, file=sys.stderr)

    base = measure_checkout_rounds(
        baseline_dir, package=pkg, bench_targets=targets,
        measure_bin=bin_path, rounds=n_rounds, bench_filter=filt,
        timeout=to, runner=runner)
    cand = measure_checkout_rounds(
        candidate_dir, package=pkg, bench_targets=targets,
        measure_bin=bin_path, rounds=n_rounds, bench_filter=filt,
        timeout=to, runner=runner)

    src = floors_source_for(base.rows.keys(), floor_map)
    # One warning when any row falls back to the default floor.
    missing = [k for k in base.rows if k not in floor_map]
    if missing:
        print(
            f"terminal floors: {len(missing)}/{len(base.rows)} row(s) lack "
            f"calibrated floors — using default {default_fl}% "
            f"(run `aro terminal-calibrate` to populate memory/floors/)",
            file=sys.stderr,
        )

    lanes = resolve_control_lanes(spec)
    families = resolve_protected_row_families(spec)
    result = judge_terminal(
        base, cand,
        epsilon_pct=eps,
        floors=floor_map,
        default_floor_pct=default_fl,
        floors_source=src,
        rounds=n_rounds,
        control_lanes=lanes or None,
        control_composition_bound_pct=(
            resolve_control_composition_bound_pct(spec) if lanes else None),
        protected_row_families=families or None,
        tradeable_regression_cap_pct=(
            resolve_tradeable_regression_cap_pct(spec) if families else None),
        protected_hysteresis=(
            resolve_protected_hysteresis(spec) if families else None),
    )
    if env_fp:
        result.env_fingerprint = env_fp
    return result


def rejudge_terminal_doc(doc: dict, *,
                         epsilon_pct: float,
                         floors: Optional[dict] = None,
                         default_floor_pct: Optional[float] = None,
                         floors_source: str = "default",
                         control_lanes: Optional[list] = None,
                         control_composition_bound_pct: Optional[float] = None,
                         protected_row_families: Optional[list] = None,
                         tradeable_regression_cap_pct: Optional[float] = None,
                         protected_hysteresis: Optional[dict] = None,
                         ) -> TerminalResult:
    """Re-adjudicate a previously written terminal.json without re-measuring.

    Rebuilds base/cand row maps from `rows[].base_ir` / `rows[].cand_ir`, then
    runs `judge_terminal` under the caller-supplied floors / lane config.
    Preserves `profile_fingerprint`, `env_fingerprint`, and `rounds` from the
    input document (measurement evidence stays with the original file).

    The input doc is verified first (`verify_terminal_doc`) so a tampered
    verdict/row cannot be laundered through rejudge into a clean output file.
    When `control_lanes` is provided (including `[]`), verification is
    lane-aware — same rule as mergeable-unlocking ingestion.
    """
    if not isinstance(doc, dict):
        raise TerminalError("rejudge: terminal doc must be a JSON object")
    # Tamper alarm before any re-adjudication output is produced.
    verify_terminal_doc(
        doc,
        control_lanes=control_lanes,
        control_bound_pct=control_composition_bound_pct,
        protected_row_families=protected_row_families,
        tradeable_regression_cap_pct=tradeable_regression_cap_pct,
        protected_hysteresis=protected_hysteresis,
    )
    row_list = doc.get("rows")
    if not isinstance(row_list, list) or not row_list:
        raise TerminalError(
            "rejudge: terminal doc has no rows[] with base_ir/cand_ir "
            "(cannot rebuild measure maps offline)")
    base_rows: dict = {}
    cand_rows: dict = {}
    for i, r in enumerate(row_list):
        if not isinstance(r, dict):
            raise TerminalError(f"rejudge: rows[{i}] is not an object")
        key = r.get("row_key")
        if not key:
            raise TerminalError(f"rejudge: rows[{i}] missing row_key")
        if "base_ir" not in r or "cand_ir" not in r:
            raise TerminalError(
                f"rejudge: rows[{i}] ({key!r}) missing base_ir/cand_ir")
        base_rows[str(key)] = int(r["base_ir"])
        cand_rows[str(key)] = int(r["cand_ir"])

    fp = str(doc.get("profile_fingerprint") or "")
    if not fp.strip():
        raise TerminalError(
            "rejudge: terminal doc missing profile_fingerprint "
            "(cannot rebuild MeasureDocs)")

    base = MeasureDoc(rows=base_rows, meta={"profile_fingerprint": fp},
                      profile_fingerprint=fp)
    cand = MeasureDoc(rows=cand_rows, meta={"profile_fingerprint": fp},
                      profile_fingerprint=fp)

    if default_floor_pct is None:
        # Prefer the historical ε on the doc when caller left default unset.
        if doc.get("epsilon_pct") is not None:
            default_floor_pct = float(doc["epsilon_pct"])
        else:
            default_floor_pct = float(epsilon_pct)

    rounds = int(doc.get("rounds") or 1)
    result = judge_terminal(
        base, cand,
        epsilon_pct=float(epsilon_pct),
        floors=floors,
        default_floor_pct=default_floor_pct,
        floors_source=floors_source if floors is not None else (
            str(doc.get("floors_source") or "default")),
        rounds=rounds,
        control_lanes=control_lanes,
        control_composition_bound_pct=control_composition_bound_pct,
        protected_row_families=protected_row_families,
        tradeable_regression_cap_pct=tradeable_regression_cap_pct,
        protected_hysteresis=protected_hysteresis,
    )
    # Preserve measurement provenance from the input evidence file.
    env_fp = str(doc.get("env_fingerprint") or "")
    if env_fp:
        result.env_fingerprint = env_fp
    result.rounds = rounds
    if doc.get("profile_fingerprint"):
        result.profile_fingerprint = str(doc["profile_fingerprint"])

    lanes = [str(x) for x in (control_lanes or [])]
    bound = control_composition_bound_pct
    if lanes and bound is None:
        bound = DEFAULT_CONTROL_COMPOSITION_BOUND_PCT
    result.notes.append(
        f"re-judged offline: control_lanes={lanes!r} "
        f"control_composition_bound_pct={bound!r} "
        f"epsilon_pct={float(epsilon_pct)} floors_source={result.floors_source}"
    )
    return result


def record_terminal(spec_name: str, result: TerminalResult, *,
                    fn: str = "terminal-gate",
                    hypothesis: str = "",
                    events_ref: str = "",
                    run_id: str = "",
                    regime: str = "terminal") -> None:
    """Write the terminal outcome through lessons + permtree (best-effort)."""
    from . import lessons as lessonsmod
    from . import permtree

    # Headline Δ: most-negative (best) row, else 0.
    best_dp = None
    if result.bench_ir_rows:
        best_dp = min(result.bench_ir_rows.values())
    note = result.notes[-1] if result.notes else result.verdict
    change = (hypothesis or f"terminal criterion Ir gate on {fn}")[:240]
    env_fp = result.env_fingerprint or None
    lessonsmod.append(
        spec_name, change, result.verdict,
        delta_pct=best_dp, note=note,
        profile_fingerprint=result.profile_fingerprint,
        env_fingerprint=env_fp,
    )
    # Surface a representative nonzero Δ as the node delta when available.
    permtree.record(
        spec_name, workload=spec_name, fn=fn, base_state="origin",
        verdict=result.verdict, regime=regime,
        delta=best_dp, hypothesis=change,
        events_ref=events_ref, run_id=run_id,
        profile_fingerprint=result.profile_fingerprint,
        env_fingerprint=env_fp,
    )


# --- calibration -------------------------------------------------------------

def run_calibrate(spec, checkout, *, rounds: int = DEFAULT_CALIBRATE_ROUNDS,
                  runner: Optional[Callable] = None,
                  measure_bin: Optional[str] = None,
                  timeout: Optional[float] = None,
                  out_path: Optional[Path] = None,
                  skip_selfcheck: bool = False,
                  version_runner: Optional[Callable] = None) -> dict:
    """Run measure N times on one checkout; write memory/floors/<spec>.json.

    Returns the payload that was written. Rebuilds are not required — floors
    are calibrated by repeated measure of a single checkout. Requires a valid
    selfcheck marker (calibrating on a broken host bakes garbage floors);
    `ARO_SKIP_SELFCHECK=1` or `skip_selfcheck=True` bypasses.
    `version_runner` injects tool-version probing for hermetic tests.
    """
    if not has_terminal_config(spec):
        raise TerminalError(
            "spec has no terminal_bench_targets — nothing to calibrate")

    env_fp = ""
    from . import selfcheck as scmod
    try:
        env_fp = scmod.require_selfcheck(
            spec, runner=version_runner, skip=skip_selfcheck) or ""
    except scmod.SelfcheckError as e:
        raise TerminalError(str(e)) from e

    n = int(rounds)
    if n < 2:
        raise TerminalError(
            "terminal-calibrate needs --rounds >= 2 (pairwise noise estimate)")
    bin_path = measure_bin if measure_bin is not None else resolve_measure_bin(spec)
    targets = terminal_bench_targets(spec)
    filt = terminal_bench_filter(spec)
    pkg = package_name(spec)
    to = timeout if timeout is not None else resolve_terminal_timeout(spec)
    min_fl = ir_epsilon_pct(spec)

    docs = [
        measure_checkout(checkout, package=pkg, bench_targets=targets,
                         measure_bin=bin_path, bench_filter=filt,
                         timeout=to, runner=runner)
        for _ in range(n)
    ]
    # Cross-round fingerprint / row-set consistency (same-side hard errors).
    median_measure_docs(docs)  # raises on drift
    floors = compute_floors_from_docs(docs, min_floor_pct=min_fl)

    name = getattr(spec, "name", None) or "unknown"
    meta = {
        "calibrated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rounds": n,
        "checkout_describe": checkout_describe(checkout),
        "measure_bin": measure_bin_label(bin_path),
        "rustc": rustc_version(),
    }
    # skip-when-absent: only attach env_fingerprint when selfcheck actually
    # produced one. Never probe fresh after a skip (would break hermeticity
    # and contradict the skip).
    if env_fp:
        meta["env_fingerprint"] = env_fp
    dest = write_floors(str(name), floors, meta=meta, path=out_path)
    return {"path": str(dest), "meta": meta, "floors": floors}


# --- CLI ---------------------------------------------------------------------

def _parse_cli_orders(args) -> Optional[set]:
    """Parse args.orders via reverify.parse_orders; SystemExit on bad input."""
    raw = getattr(args, "orders", None)
    if raw is None or str(raw).strip() == "":
        return None
    from .reverify import parse_orders
    try:
        return parse_orders(raw)
    except ValueError as e:
        raise SystemExit(f"aro terminal: invalid --orders: {e}") from e


def terminal_doc_dict(result, measured_orders=None, baseline_sha=None) -> dict:
    """TerminalResult/dict → JSON-ready doc; optional sorted measured_orders.

    ``baseline_sha`` is the resolved commit the measured worktrees were built
    from (SpecTarget.baseline_sha). Omitted only when genuinely unavailable
    (legacy docs / synthetic fixtures); ship gate fails closed on absence.
    """
    if hasattr(result, "to_dict"):
        d = result.to_dict()
    else:
        d = dict(result)
    if measured_orders is not None:
        d["measured_orders"] = sorted(int(x) for x in measured_orders)
    if baseline_sha:
        d["baseline_sha"] = str(baseline_sha)
    return d


def resolve_measure_baseline_sha(spec, baseline_dir=None) -> Optional[str]:
    """Best-effort resolved baseline sha for a terminal measure doc.

    Prefers SpecTarget's pin (the sha worktrees are cut from). Falls back to
    HEAD of the baseline checkout, then rev-parse of ``spec.baseline_ref`` in
    the target repo. Returns None only when none of those resolve.
    """
    from . import vcs as vcsmod

    try:
        from .target import SpecTarget
        sha = SpecTarget(spec).baseline_sha
        if sha:
            # SpecTarget falls back to the raw ref string when rev-parse fails;
            # only keep full-length hex shas as "resolved".
            if len(str(sha)) >= 40 and all(
                    c in "0123456789abcdef" for c in str(sha).lower()):
                return str(sha)
    except Exception:
        pass
    if baseline_dir is not None:
        sha = vcsmod.rev_parse(baseline_dir, "HEAD")
        if sha:
            return sha
    try:
        repo = getattr(spec, "repo", None)
        bref = getattr(spec, "baseline_ref", None)
        if repo is not None and bref:
            sha = vcsmod.rev_parse(repo, bref)
            if sha:
                return sha
    except Exception:
        pass
    return None


def _effective_measured_orders(doc: dict, explicit: Optional[set]) -> Optional[set]:
    """Rejudge membership: explicit --orders wins over doc.measured_orders."""
    if explicit is not None:
        return explicit
    mo = doc.get("measured_orders") if isinstance(doc, dict) else None
    if mo is None:
        return None
    return {int(x) for x in mo}


def cli(args) -> None:
    """`aro terminal <spec> --baseline DIR --candidate DIR` (or --list / --rejudge)."""
    from . import manifest as manifestmod
    from . import spec as specmod

    rejudge_path = getattr(args, "rejudge", None)
    list_only = bool(getattr(args, "list", False) or getattr(args, "dry_run", False))
    # Resolved once for measure + rejudge write paths (None = legacy / all).
    cli_orders = _parse_cli_orders(args)

    if rejudge_path and list_only:
        raise SystemExit("aro terminal: --rejudge is mutually exclusive with --list/--dry-run")
    if rejudge_path and (getattr(args, "baseline", None) or getattr(args, "candidate", None)):
        raise SystemExit(
            "aro terminal: --rejudge is mutually exclusive with --baseline/--candidate "
            "(offline re-adjudication does not re-measure)")

    # --list / dry: never need the measure binary or worktree dirs.
    if list_only:
        # Load via from_dict when possible so a missing target_repo path does
        # not break list mode (same discipline as recheck-debts --list-only).
        raw = json.loads(Path(args.spec).read_text())
        sp = specmod.from_dict(raw)
        targets = terminal_bench_targets(sp)
        filt = terminal_bench_filter(sp)
        lanes = resolve_control_lanes(sp)
        print(f"terminal config for {sp.name}:")
        print(f"  terminal_bench_targets: {targets or '(none — gate disabled)'}")
        print(f"  terminal_bench_filter:  {filt or '(none)'}")
        print(f"  icount_epsilon_pct:     {ir_epsilon_pct(sp)}")
        print(f"  terminal_measure_rounds:{resolve_terminal_rounds(sp)}")
        print(f"  terminal_default_floor: {resolve_default_floor_pct(sp)}%")
        print(f"  control_lanes:          {lanes or '(none — all rows subject)'}")
        if lanes:
            print(f"  control_composition_bound_pct: "
                  f"{resolve_control_composition_bound_pct(sp)}%")
        families = resolve_protected_row_families(sp)
        print(f"  protected_row_families: {families or '(none — no trade policy)'}")
        if families:
            print(f"  tradeable_regression_cap_pct: "
                  f"{resolve_tradeable_regression_cap_pct(sp)}")
            print(f"  protected_hysteresis:   {resolve_protected_hysteresis(sp)}")
        fp = floors_path(sp.name)
        print(f"  floors_file:            {fp}"
              + (" (present)" if fp.is_file() else " (missing — defaults)"))
        try:
            print(f"  measure_bin:            {resolve_measure_bin(sp)}")
        except TerminalError as e:
            print(f"  measure_bin:            UNSET ({e})")
        try:
            print(f"  package:                {package_name(sp)}")
        except TerminalError as e:
            print(f"  package:                UNSET ({e})")
        print(f"  gate active:            {has_terminal_config(sp)}")
        return

    # Offline re-judge: load terminal.json, re-adjudicate with current spec config.
    if rejudge_path:
        try:
            sp = specmod.load(args.spec)
        except Exception:
            raw = json.loads(Path(args.spec).read_text())
            sp = specmod.from_dict(raw)

        in_path = Path(rejudge_path)
        if not in_path.is_file():
            raise SystemExit(f"aro terminal --rejudge: no file at {in_path}")
        try:
            doc = json.loads(in_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"aro terminal --rejudge: failed to read {in_path}: {e}")

        # Lane-aware verify before any output path is written (spec always
        # available as positional arg). Empty control_lanes → any control-*
        # stored status is itself an error. Policy fields when declared.
        lanes = resolve_control_lanes(sp)
        bound = (resolve_control_composition_bound_pct(sp) if lanes else None)
        families = resolve_protected_row_families(sp)
        cap = (resolve_tradeable_regression_cap_pct(sp) if families else None)
        hyst = (resolve_protected_hysteresis(sp) if families else None)
        try:
            verify_terminal_doc(
                doc if isinstance(doc, dict) else {},
                control_lanes=lanes,
                control_bound_pct=bound,
                protected_row_families=families or None,
                tradeable_regression_cap_pct=cap,
                protected_hysteresis=hyst,
            )
        except TerminalError as e:
            print(f"terminal rejudge ERROR: {e}", file=sys.stderr)
            raise SystemExit(2)

        old_verdict = doc.get("verdict") if isinstance(doc, dict) else None
        eps = ir_epsilon_pct(sp)
        default_fl = resolve_default_floor_pct(sp)
        name = getattr(sp, "name", None) or "unknown"
        floor_map, _meta, floor_warnings = load_floors(str(name))
        for w in floor_warnings:
            print(w, file=sys.stderr)
        src = floors_source_for(
            [r.get("row_key") for r in (doc.get("rows") or [])
             if isinstance(r, dict) and r.get("row_key")],
            floor_map,
        )
        try:
            result = rejudge_terminal_doc(
                doc,
                epsilon_pct=eps,
                floors=floor_map,
                default_floor_pct=default_fl,
                floors_source=src,
                control_lanes=lanes,
                control_composition_bound_pct=bound,
                protected_row_families=families or None,
                tradeable_regression_cap_pct=cap,
                protected_hysteresis=hyst,
            )
        except TerminalError as e:
            print(f"terminal rejudge ERROR: {e}", file=sys.stderr)
            raise SystemExit(2)

        # Effective membership: explicit --orders wins over doc.measured_orders.
        # Embed in .rejudged.json so stamp source sha256 matches stamp semantics.
        # Preserve baseline_sha from the input evidence file (offline re-adjudication
        # does not re-measure; the certified baseline pin is unchanged).
        measured = _effective_measured_orders(
            doc if isinstance(doc, dict) else {}, cli_orders)
        prior_bsha = (doc.get("baseline_sha")
                      if isinstance(doc, dict) else None)
        rejudge_doc = terminal_doc_dict(
            result, measured_orders=measured, baseline_sha=prior_bsha)

        out_path = Path(str(in_path) + ".rejudged.json")
        out_path.write_text(
            json.dumps(rejudge_doc, ensure_ascii=False, indent=1) + "\n")
        print(f"terminal rejudge: {old_verdict} → {result.verdict}")
        print(f"  input (unmodified): {in_path}")
        print(f"  output:             {out_path}")
        print(f"  profile_fingerprint: {result.profile_fingerprint}")
        if result.env_fingerprint:
            print(f"  env_fingerprint:     {result.env_fingerprint}")
        print(f"  rounds: {result.rounds}  floors_source: {result.floors_source}")
        print(f"  control_lanes: {lanes or '(none)'}")
        if lanes:
            print(f"  control_composition_bound_pct: "
                  f"{resolve_control_composition_bound_pct(sp)}%")
        if measured is not None:
            print(f"  measured_orders:     {sorted(measured)}")
        print(f"  nonzero Δ rows: {len(result.bench_ir_rows)}")
        for k, dp in sorted(result.bench_ir_rows.items(), key=lambda kv: abs(kv[1]),
                            reverse=True):
            print(f"    {k}: {dp:+.4f}%")
        for n in result.notes:
            print(f"  note: {n}")
        # Optional write-back through the SAME apply_terminal path measure uses
        # (no new stamping code). Stamp source is the rejudged output file.
        um = getattr(args, "update_manifest", None)
        if um:
            mpath = Path(um)
            if mpath.is_dir():
                mpath = mpath / "manifest.json"
            oq = float(getattr(
                sp, "outlier_quarantine_pct",
                manifestmod.DEFAULT_OUTLIER_QUARANTINE_PCT))
            um_lanes = lanes
            um_bound = bound
            um_families = families
            um_cap = cap
            um_hyst = hyst
            if not mpath.exists():
                run_dir = Path(um) if Path(um).is_dir() else mpath.parent
                m = manifestmod.build_manifest(
                    run_dir, terminal_result=rejudge_doc,
                    terminal_required=has_terminal_config(sp),
                    outlier_quarantine_pct=oq,
                    terminal_source=str(out_path),
                    control_lanes=um_lanes,
                    control_bound_pct=um_bound,
                    protected_row_families=um_families or None,
                    tradeable_regression_cap_pct=um_cap,
                    protected_hysteresis=um_hyst)
                # build_manifest does not yet honor measured_orders; re-stamp
                # via apply_terminal so membership is enforced when a new
                # manifest is materialised from events.
                m = manifestmod.apply_terminal(
                    m, rejudge_doc, terminal_required=has_terminal_config(sp),
                    outlier_quarantine_pct=oq,
                    source=str(out_path),
                    control_lanes=um_lanes,
                    control_bound_pct=um_bound,
                    protected_row_families=um_families or None,
                    tradeable_regression_cap_pct=um_cap,
                    protected_hysteresis=um_hyst,
                    orders=measured)
                dest = run_dir / "manifest.json"
            else:
                m = json.loads(mpath.read_text())
                m = manifestmod.apply_terminal(
                    m, rejudge_doc, terminal_required=has_terminal_config(sp),
                    outlier_quarantine_pct=oq,
                    source=str(out_path),
                    control_lanes=um_lanes,
                    control_bound_pct=um_bound,
                    protected_row_families=um_families or None,
                    tradeable_regression_cap_pct=um_cap,
                    protected_hysteresis=um_hyst,
                    orders=measured)
                dest = mpath
            dest.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
            ok = sum(1 for a in m.get("accepted", []) if a.get("mergeable"))
            print(f"  manifest updated → {dest} ({ok} mergeable)")
        emit_verdict_next(result.verdict)
        if not is_mergeable_terminal_verdict(result.verdict):
            print(f"  (PR blocked: {result.verdict})", file=sys.stderr)
        return

    if not getattr(args, "baseline", None) or not getattr(args, "candidate", None):
        raise SystemExit(
            "aro terminal: --baseline and --candidate are required "
            "(or pass --list / --rejudge PATH)")

    try:
        sp = specmod.load(args.spec)
    except Exception:
        # Fall back to from_dict so tests with synthetic fixture specs work
        # without a real target checkout for probe-file validation.
        raw = json.loads(Path(args.spec).read_text())
        sp = specmod.from_dict(raw)

    try:
        result = run_terminal(sp, args.baseline, args.candidate)
    except TerminalError as e:
        print(f"terminal gate ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)

    measure_bsha = resolve_measure_baseline_sha(sp, args.baseline)
    measure_doc = terminal_doc_dict(
        result, measured_orders=cli_orders, baseline_sha=measure_bsha)
    out_path = getattr(args, "out", None)
    if out_path:
        Path(out_path).write_text(
            json.dumps(measure_doc, ensure_ascii=False, indent=1) + "\n")
        print(f"terminal → {out_path}")

    print(f"terminal verdict: {result.verdict}")
    print(f"  profile_fingerprint: {result.profile_fingerprint}")
    if result.env_fingerprint:
        print(f"  env_fingerprint:     {result.env_fingerprint}")
    print(f"  rounds: {result.rounds}  floors_source: {result.floors_source}")
    if cli_orders is not None:
        print(f"  measured_orders:     {sorted(cli_orders)}")
    print(f"  nonzero Δ rows: {len(result.bench_ir_rows)}")
    for k, dp in sorted(result.bench_ir_rows.items(), key=lambda kv: abs(kv[1]),
                        reverse=True):
        print(f"    {k}: {dp:+.4f}%")
    for n in result.notes:
        print(f"  note: {n}")
    emit_verdict_next(result.verdict)

    if getattr(args, "record", False):
        record_terminal(
            sp.name, result,
            fn=getattr(args, "fn", None) or "terminal-gate",
            hypothesis=getattr(args, "hypothesis", None) or "",
            events_ref=getattr(args, "events_ref", None) or "",
        )
        print("  recorded → lessons + permtree")

    um = getattr(args, "update_manifest", None)
    if um:
        mpath = Path(um)
        if mpath.is_dir():
            mpath = mpath / "manifest.json"
        # Default-ON outlier tripwire from the loaded spec (explicit 0 disables).
        oq = float(getattr(
            sp, "outlier_quarantine_pct",
            manifestmod.DEFAULT_OUTLIER_QUARANTINE_PCT))
        # Stamp needs a terminal.json on disk (sha256 of file bytes). Prefer
        # --out; otherwise write one next to the manifest so the stamp is real.
        # Always write measure_doc (includes measured_orders when set) so the
        # stamp source bytes match membership semantics.
        term_source = out_path
        if not term_source:
            run_dir_for_term = Path(um) if Path(um).is_dir() else mpath.parent
            term_source = str(run_dir_for_term / "terminal.json")
            Path(term_source).write_text(
                json.dumps(measure_doc, ensure_ascii=False, indent=1) + "\n")
            print(f"terminal → {term_source}")
        elif not Path(term_source).is_file():
            # --out path was set but write above already happened; no-op.
            pass
        # Mergeable-unlocking stamp path: always lane-aware (spec is loaded).
        # Spec without control_lanes → control_lanes=[] (any control-* errors).
        um_lanes = resolve_control_lanes(sp)
        um_bound = (
            resolve_control_composition_bound_pct(sp) if um_lanes else None)
        um_families = resolve_protected_row_families(sp)
        um_cap = (
            resolve_tradeable_regression_cap_pct(sp) if um_families else None)
        um_hyst = (
            resolve_protected_hysteresis(sp) if um_families else None)
        if not mpath.exists():
            # Build from the run dir if a bare out-dir was given.
            run_dir = Path(um) if Path(um).is_dir() else mpath.parent
            m = manifestmod.build_manifest(
                run_dir, terminal_result=measure_doc,
                terminal_required=has_terminal_config(sp),
                outlier_quarantine_pct=oq,
                terminal_source=term_source,
                control_lanes=um_lanes,
                control_bound_pct=um_bound,
                protected_row_families=um_families or None,
                tradeable_regression_cap_pct=um_cap,
                protected_hysteresis=um_hyst)
            m = manifestmod.apply_terminal(
                m, measure_doc, terminal_required=has_terminal_config(sp),
                outlier_quarantine_pct=oq,
                source=term_source,
                control_lanes=um_lanes,
                control_bound_pct=um_bound,
                protected_row_families=um_families or None,
                tradeable_regression_cap_pct=um_cap,
                protected_hysteresis=um_hyst,
                orders=cli_orders)
            dest = run_dir / "manifest.json"
        else:
            m = json.loads(mpath.read_text())
            m = manifestmod.apply_terminal(
                m, measure_doc, terminal_required=has_terminal_config(sp),
                outlier_quarantine_pct=oq,
                source=term_source,
                control_lanes=um_lanes,
                control_bound_pct=um_bound,
                protected_row_families=um_families or None,
                tradeable_regression_cap_pct=um_cap,
                protected_hysteresis=um_hyst,
                orders=cli_orders)
            dest = mpath
        dest.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        ok = sum(1 for a in m.get("accepted", []) if a.get("mergeable"))
        print(f"  manifest updated → {dest} ({ok} mergeable)")

    # Non-mergeable is a soft block for the operator protocol, not a process
    # crash — exit 0 so scripts can read the JSON. The PR path checks the verdict.
    if not is_mergeable_terminal_verdict(result.verdict):
        print(f"  (PR blocked: {result.verdict})", file=sys.stderr)


def calibrate_cli(args) -> None:
    """`aro terminal-calibrate <spec> --checkout DIR [--rounds N] [--dry-run]`."""
    from . import spec as specmod

    raw = json.loads(Path(args.spec).read_text())
    try:
        sp = specmod.from_dict(raw)
    except Exception:
        try:
            sp = specmod.load(args.spec)
        except Exception as e:
            raise SystemExit(f"terminal-calibrate: failed to load spec: {e}")

    checkout = getattr(args, "checkout", None)
    if not checkout:
        raise SystemExit("terminal-calibrate: --checkout DIR is required")

    rounds = int(getattr(args, "rounds", None) or DEFAULT_CALIBRATE_ROUNDS)
    dry = bool(getattr(args, "dry_run", False))

    if dry:
        # Never need the measure binary for dry-run; still resolve when present
        # so the printed command is complete, but missing bin is not fatal.
        try:
            bin_path = resolve_measure_bin(sp)
        except TerminalError:
            bin_path = "<measure_bin UNSET>"
        try:
            pkg = package_name(sp)
        except TerminalError as e:
            pkg = f"UNSET ({e})"
        targets = terminal_bench_targets(sp)
        filt = terminal_bench_filter(sp)
        cmd = build_measure_cmd(
            bin_path if not bin_path.startswith("<") else "MEASURE_BIN",
            checkout, package=pkg if not str(pkg).startswith("UNSET") else "PKG",
            bench_targets=targets or ["<no terminal_bench_targets>"],
            bench_filter=filt)
        print(f"terminal-calibrate dry-run for {getattr(sp, 'name', '?')}:")
        print(f"  checkout:  {checkout}")
        print(f"  rounds:    {rounds}")
        print(f"  package:   {pkg}")
        print(f"  targets:   {targets or '(none)'}")
        print(f"  filter:    {filt or '(none)'}")
        print(f"  measure:   {bin_path}")
        print(f"  cmd:       {' '.join(str(c) for c in cmd)}")
        print(f"  would write floors → {floors_path(getattr(sp, 'name', 'unknown'))}")
        print(f"  floor formula: max_pairwise|Δ%| × {FLOOR_SAFETY_FACTOR} "
              f"(clamped to ir_epsilon_pct={ir_epsilon_pct(sp)})")
        return

    try:
        payload = run_calibrate(
            sp, checkout, rounds=rounds,
            measure_bin=getattr(args, "measure_bin", None) or None)
    except TerminalError as e:
        print(f"terminal-calibrate ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)

    print(f"terminal-calibrate → {payload['path']}")
    print(f"  rounds={payload['meta']['rounds']}  "
          f"rows={len(payload['floors'])}  "
          f"rustc={payload['meta'].get('rustc')!r}")
    # Show a short top-of-floors summary (largest floors first).
    top = sorted(payload["floors"].items(), key=lambda kv: kv[1], reverse=True)[:8]
    for k, fl in top:
        print(f"    {k}: {fl:.4f}%")
    if len(payload["floors"]) > 8:
        print(f"    … +{len(payload['floors']) - 8} more rows")
