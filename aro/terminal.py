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
from .stats import median as _median

# --- verdict vocabulary (pre-PR terminal gate; not evaluate() outcomes) ------

TERMINAL_CONFIRMED = "TERMINAL_CONFIRMED"   # ≥1 row improved, none regressed beyond floor
TERMINAL_UNTOUCHED = "TERMINAL_UNTOUCHED"   # every row |Δ| ≤ floor → block PR (#326/#332)
TERMINAL_REGRESSED = "TERMINAL_REGRESSED"   # ≥1 row worse beyond floor, none improved
TERMINAL_MIXED = "TERMINAL_MIXED"           # improvements AND regressions → operator call

ALL_TERMINAL_VERDICTS = frozenset({
    TERMINAL_CONFIRMED, TERMINAL_UNTOUCHED, TERMINAL_REGRESSED, TERMINAL_MIXED,
})

# Cap the manifest's nonzero-Δ summary so PR bodies stay short.
_MAX_BENCH_IR_ROWS = 32

# Floor calibration: max pairwise |Δ%| × safety, clamped to probe ε minimum.
FLOOR_SAFETY_FACTOR = 2.0
DEFAULT_TERMINAL_ROUNDS = 3
DEFAULT_TERMINAL_FLOOR_PCT = 1.0
DEFAULT_CALIBRATE_ROUNDS = 4
FLOORS_STALE_DAYS = 30

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
    status: str         # improved | untouched | regressed
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

    def to_dict(self) -> dict:
        return {
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


# --- config resolution -------------------------------------------------------

def has_terminal_config(spec) -> bool:
    """True when the target declares terminal_bench_targets (terminal gate on)."""
    if spec is None:
        return False
    targets = getattr(spec, "terminal_bench_targets", None)
    if targets:
        return True
    raw = getattr(spec, "raw", None) or {}
    return bool(raw.get("terminal_bench_targets"))


def terminal_bench_targets(spec) -> list:
    t = getattr(spec, "terminal_bench_targets", None)
    if t:
        return list(t)
    raw = getattr(spec, "raw", None) or {}
    return list(raw.get("terminal_bench_targets") or [])


def terminal_bench_filter(spec) -> Optional[str]:
    f = getattr(spec, "terminal_bench_filter", None)
    if f:
        return str(f)
    raw = getattr(spec, "raw", None) or {}
    v = raw.get("terminal_bench_filter")
    return str(v) if v else None


def resolve_measure_bin(spec=None) -> str:
    """Binary path for `mega-bench-reporter`. Env wins over target JSON.

    Raises TerminalError with a clear message when unset — never a traceback
    about NoneType paths.
    """
    env = os.environ.get("ARO_MEASURE_BIN")
    if env is not None and str(env).strip():
        return str(env).strip()
    if spec is not None:
        mb = getattr(spec, "measure_bin", None)
        if mb is not None and str(mb).strip():
            return str(mb).strip()
        raw = getattr(spec, "raw", None) or {}
        if raw.get("measure_bin"):
            return str(raw["measure_bin"]).strip()
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
    v = getattr(spec, "terminal_timeout_secs", None)
    if v is None:
        raw = getattr(spec, "raw", None) or {}
        v = raw.get("terminal_timeout_secs")
    if v is not None and str(v).strip() != "":
        return float(v)
    base = getattr(spec, "timeout", None)
    if base is None:
        raw = getattr(spec, "raw", None) or {}
        base = raw.get("timeout", 1800)
    return 4.0 * float(base)


def resolve_terminal_rounds(spec=None) -> int:
    """How many times to measure each side. Env ARO_TERMINAL_ROUNDS wins."""
    env = os.environ.get("ARO_TERMINAL_ROUNDS")
    if env is not None and str(env).strip() != "":
        n = int(env)
        if n < 1:
            raise TerminalError("ARO_TERMINAL_ROUNDS must be >= 1")
        return n
    if spec is not None:
        v = getattr(spec, "terminal_measure_rounds", None)
        if v is None:
            raw = getattr(spec, "raw", None) or {}
            v = raw.get("terminal_measure_rounds")
        if v is not None and str(v).strip() != "":
            n = int(v)
            if n < 1:
                raise TerminalError("terminal_measure_rounds must be >= 1")
            return n
    return DEFAULT_TERMINAL_ROUNDS


def resolve_default_floor_pct(spec=None) -> float:
    """Conservative floor when a row has no calibrated entry. Default 1.0%."""
    if spec is not None:
        v = getattr(spec, "terminal_default_floor_pct", None)
        if v is None:
            raw = getattr(spec, "raw", None) or {}
            v = raw.get("terminal_default_floor_pct")
        if v is not None and str(v).strip() != "":
            return float(v)
    return DEFAULT_TERMINAL_FLOOR_PCT


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


def judge_terminal(base: MeasureDoc, cand: MeasureDoc, *,
                   epsilon_pct: float,
                   floors: Optional[dict] = None,
                   default_floor_pct: Optional[float] = None,
                   floors_source: str = "default",
                   rounds: int = 1) -> TerminalResult:
    """Diff two measure docs into a TERMINAL_* verdict.

    Per-row threshold is floor(row): calibrated value when present, else
    `default_floor_pct`. When `default_floor_pct` is None, falls back to
    `epsilon_pct` so callers that pass only ε keep the legacy single-threshold
    behaviour (floors all = ε).

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

    rows: list = []
    improved = regressed = 0
    nonzero: dict = {}
    for k in sorted(base_keys):
        b, c = int(base.rows[k]), int(cand.rows[k])
        dp = _row_delta_pct(b, c)
        fl = float(floor_map[k]) if k in floor_map else default_floor_pct
        if dp < -fl:
            status = "improved"
            improved += 1
        elif dp > fl:
            status = "regressed"
            regressed += 1
        else:
            status = "untouched"
        rows.append(RowDelta(k, b, c, dp, status, floor_pct=fl))
        if b != c:
            nonzero[k] = round(dp, 4)

    # Cap the summary map (stable order: largest |Δ| first).
    if len(nonzero) > _MAX_BENCH_IR_ROWS:
        top = sorted(nonzero.items(), key=lambda kv: abs(kv[1]), reverse=True)
        nonzero = dict(top[:_MAX_BENCH_IR_ROWS])

    notes = [
        f"terminal gate: rows={len(rows)} improved={improved} "
        f"regressed={regressed} floors_source={src} rounds={int(rounds)} "
        f"fp={base.profile_fingerprint!r}",
    ]
    if improved and not regressed:
        verdict = TERMINAL_CONFIRMED
        notes.append("verdict: TERMINAL_CONFIRMED — ≥1 criterion row improved, none regressed")
    elif not improved and not regressed:
        verdict = TERMINAL_UNTOUCHED
        notes.append(
            "verdict: TERMINAL_UNTOUCHED — every criterion row |ΔIr| ≤ floor "
            "(probe-vs-bench divergence; block PR — #326/#332 shape)")
    elif regressed and not improved:
        verdict = TERMINAL_REGRESSED
        notes.append("verdict: TERMINAL_REGRESSED — ≥1 criterion row worse beyond floor")
    else:
        verdict = TERMINAL_MIXED
        notes.append(
            "verdict: TERMINAL_MIXED — improvements AND regressions; "
            "blocked pending operator decision")

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
                 floors_path_override: Optional[Path] = None) -> TerminalResult:
    """Measure both worktrees (median-of-N) and adjudicate with per-row floors.

    Pure of lessons/permtree I/O. Floors file missing → default floor for every
    row + one stderr warning (does not block; a later selfcheck ticket gates).
    """
    if not has_terminal_config(spec):
        raise TerminalError(
            "spec has no terminal_bench_targets — terminal gate not configured "
            "(add the field to the target JSON or skip the gate)")
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

    return judge_terminal(
        base, cand,
        epsilon_pct=eps,
        floors=floor_map,
        default_floor_pct=default_fl,
        floors_source=src,
        rounds=n_rounds,
    )


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
    lessonsmod.append(
        spec_name, change, result.verdict,
        delta_pct=best_dp, note=note,
        profile_fingerprint=result.profile_fingerprint,
    )
    # Surface a representative nonzero Δ as the node delta when available.
    permtree.record(
        spec_name, workload=spec_name, fn=fn, base_state="origin",
        verdict=result.verdict, regime=regime,
        delta=best_dp, hypothesis=change,
        events_ref=events_ref, run_id=run_id,
        profile_fingerprint=result.profile_fingerprint,
    )


# --- calibration -------------------------------------------------------------

def run_calibrate(spec, checkout, *, rounds: int = DEFAULT_CALIBRATE_ROUNDS,
                  runner: Optional[Callable] = None,
                  measure_bin: Optional[str] = None,
                  timeout: Optional[float] = None,
                  out_path: Optional[Path] = None) -> dict:
    """Run measure N times on one checkout; write memory/floors/<spec>.json.

    Returns the payload that was written. Rebuilds are not required — floors
    are calibrated by repeated measure of a single checkout.
    """
    if not has_terminal_config(spec):
        raise TerminalError(
            "spec has no terminal_bench_targets — nothing to calibrate")
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
    dest = write_floors(str(name), floors, meta=meta, path=out_path)
    return {"path": str(dest), "meta": meta, "floors": floors}


# --- CLI ---------------------------------------------------------------------

def cli(args) -> None:
    """`aro terminal <spec> --baseline DIR --candidate DIR` (or --list)."""
    from . import manifest as manifestmod
    from . import spec as specmod

    # --list / dry: never need the measure binary or worktree dirs.
    list_only = bool(getattr(args, "list", False) or getattr(args, "dry_run", False))
    if list_only:
        # Load via from_dict when possible so a missing target_repo path does
        # not break list mode (same discipline as recheck-debts --list-only).
        raw = json.loads(Path(args.spec).read_text())
        sp = specmod.from_dict(raw)
        targets = terminal_bench_targets(sp)
        filt = terminal_bench_filter(sp)
        print(f"terminal config for {sp.name}:")
        print(f"  terminal_bench_targets: {targets or '(none — gate disabled)'}")
        print(f"  terminal_bench_filter:  {filt or '(none)'}")
        print(f"  icount_epsilon_pct:     {ir_epsilon_pct(sp)}")
        print(f"  terminal_measure_rounds:{resolve_terminal_rounds(sp)}")
        print(f"  terminal_default_floor: {resolve_default_floor_pct(sp)}%")
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

    if not getattr(args, "baseline", None) or not getattr(args, "candidate", None):
        raise SystemExit(
            "aro terminal: --baseline and --candidate are required "
            "(or pass --list to inspect config without measuring)")

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

    out_path = getattr(args, "out", None)
    if out_path:
        Path(out_path).write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=1) + "\n")
        print(f"terminal → {out_path}")

    print(f"terminal verdict: {result.verdict}")
    print(f"  profile_fingerprint: {result.profile_fingerprint}")
    print(f"  rounds: {result.rounds}  floors_source: {result.floors_source}")
    print(f"  nonzero Δ rows: {len(result.bench_ir_rows)}")
    for k, dp in sorted(result.bench_ir_rows.items(), key=lambda kv: abs(kv[1]),
                        reverse=True):
        print(f"    {k}: {dp:+.4f}%")
    for n in result.notes:
        print(f"  note: {n}")

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
        if not mpath.exists():
            # Build from the run dir if a bare out-dir was given.
            run_dir = Path(um) if Path(um).is_dir() else mpath.parent
            m = manifestmod.build_manifest(
                run_dir, terminal_result=result,
                terminal_required=has_terminal_config(sp))
            dest = run_dir / "manifest.json"
        else:
            m = json.loads(mpath.read_text())
            m = manifestmod.apply_terminal(
                m, result, terminal_required=has_terminal_config(sp))
            dest = mpath
        dest.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        ok = sum(1 for a in m.get("accepted", []) if a.get("mergeable"))
        print(f"  manifest updated → {dest} ({ok} mergeable)")

    # Non-CONFIRMED is a soft block for the operator protocol, not a process
    # crash — exit 0 so scripts can read the JSON. The PR path checks the verdict.
    if result.verdict != TERMINAL_CONFIRMED:
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
