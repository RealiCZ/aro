"""Terminal criterion-Ir gate (pre-PR).

Probe-level Ir wins do not imply criterion bench wins (coverage and weights
differ). Before opening a perf PR, measure both the baseline and candidate
worktrees via the external `mega-bench-reporter measure --instructions` CLI
and diff every row's instruction count — the same signal CodSpeed CI reports.

Intercepts the #326/#332 failure shape: a probe Ir win that moves zero
criterion rows must never become a PR. Spec: ARO_ICOUNT_GATE_PLAN §4.

The measure binary is not available on all hosts (macOS / no valgrind). Tests
inject a `runner` callable that returns fixture JSON; production uses the real
CLI. `ARO_MEASURE_BIN` wins over the target JSON `measure_bin` field.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .icount import ir_epsilon_pct

# --- verdict vocabulary (pre-PR terminal gate; not evaluate() outcomes) ------

TERMINAL_CONFIRMED = "TERMINAL_CONFIRMED"   # ≥1 row improved, none regressed beyond ε
TERMINAL_UNTOUCHED = "TERMINAL_UNTOUCHED"   # every row |Δ| ≤ ε → block PR (#326/#332)
TERMINAL_REGRESSED = "TERMINAL_REGRESSED"   # ≥1 row worse beyond ε, none improved
TERMINAL_MIXED = "TERMINAL_MIXED"           # improvements AND regressions → operator call

ALL_TERMINAL_VERDICTS = frozenset({
    TERMINAL_CONFIRMED, TERMINAL_UNTOUCHED, TERMINAL_REGRESSED, TERMINAL_MIXED,
})

# Cap the manifest's nonzero-Δ summary so PR bodies stay short.
_MAX_BENCH_IR_ROWS = 32


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


@dataclass
class TerminalResult:
    """Outcome of one baseline-vs-candidate terminal measurement."""
    verdict: str
    bench_ir_rows: dict          # row_key -> delta_pct for nonzero Δ (capped)
    profile_fingerprint: str
    rows: list = field(default_factory=list)   # list[RowDelta]
    notes: list = field(default_factory=list)
    epsilon_pct: float = 0.1

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "bench_ir_rows": dict(self.bench_ir_rows),
            "profile_fingerprint": self.profile_fingerprint,
            "epsilon_pct": self.epsilon_pct,
            "notes": list(self.notes),
            "rows": [
                {"row_key": r.row_key, "base_ir": r.base_ir, "cand_ir": r.cand_ir,
                 "delta_pct": r.delta_pct, "status": r.status}
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
    fp = str(meta.get("profile_fingerprint") or "")
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


def _default_runner(cmd: list) -> tuple:
    """(stdout, stderr, returncode) via subprocess. Injectable for tests."""
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.stdout, p.stderr, p.returncode


def measure_checkout(checkout, *, package: str, bench_targets: list,
                     measure_bin: str, bench_filter: Optional[str] = None,
                     runner: Optional[Callable] = None) -> MeasureDoc:
    """Invoke measure on one worktree and parse its JSON stdout."""
    if not bench_targets:
        raise TerminalError("terminal_bench_targets is empty — nothing to measure")
    cmd = build_measure_cmd(measure_bin, checkout, package=package,
                            bench_targets=bench_targets, bench_filter=bench_filter)
    run = runner or _default_runner
    stdout, stderr, rc = run(cmd)
    if rc != 0:
        msg = (stderr or stdout or "").strip() or f"exit {rc}"
        raise TerminalError(f"measure failed (exit {rc}): {msg}")
    return parse_measure_stdout(stdout)


# --- adjudication ------------------------------------------------------------

def _row_delta_pct(base_ir: int, cand_ir: int) -> float:
    if base_ir == 0:
        if cand_ir == 0:
            return 0.0
        raise TerminalError(
            f"baseline instr_count is 0 with candidate={cand_ir} — measurement unusable")
    return (cand_ir - base_ir) / base_ir * 100.0


def judge_terminal(base: MeasureDoc, cand: MeasureDoc, *,
                   epsilon_pct: float) -> TerminalResult:
    """Diff two measure docs into a TERMINAL_* verdict.

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

    rows: list = []
    improved = regressed = 0
    nonzero: dict = {}
    for k in sorted(base_keys):
        b, c = int(base.rows[k]), int(cand.rows[k])
        dp = _row_delta_pct(b, c)
        if dp < -epsilon_pct:
            status = "improved"
            improved += 1
        elif dp > epsilon_pct:
            status = "regressed"
            regressed += 1
        else:
            status = "untouched"
        rows.append(RowDelta(k, b, c, dp, status))
        if b != c:
            nonzero[k] = round(dp, 4)

    # Cap the summary map (stable order: largest |Δ| first).
    if len(nonzero) > _MAX_BENCH_IR_ROWS:
        top = sorted(nonzero.items(), key=lambda kv: abs(kv[1]), reverse=True)
        nonzero = dict(top[:_MAX_BENCH_IR_ROWS])

    notes = [
        f"terminal gate: rows={len(rows)} improved={improved} "
        f"regressed={regressed} ε={epsilon_pct}% "
        f"fp={base.profile_fingerprint!r}",
    ]
    if improved and not regressed:
        verdict = TERMINAL_CONFIRMED
        notes.append("verdict: TERMINAL_CONFIRMED — ≥1 criterion row improved, none regressed")
    elif not improved and not regressed:
        verdict = TERMINAL_UNTOUCHED
        notes.append(
            "verdict: TERMINAL_UNTOUCHED — every criterion row |ΔIr| ≤ ε "
            "(probe-vs-bench divergence; block PR — #326/#332 shape)")
    elif regressed and not improved:
        verdict = TERMINAL_REGRESSED
        notes.append("verdict: TERMINAL_REGRESSED — ≥1 criterion row worse beyond ε")
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
    )


def run_terminal(spec, baseline_dir, candidate_dir, *,
                 runner: Optional[Callable] = None,
                 measure_bin: Optional[str] = None) -> TerminalResult:
    """Measure both worktrees and adjudicate. Pure of lessons/permtree I/O."""
    if not has_terminal_config(spec):
        raise TerminalError(
            "spec has no terminal_bench_targets — terminal gate not configured "
            "(add the field to the target JSON or skip the gate)")
    bin_path = measure_bin if measure_bin is not None else resolve_measure_bin(spec)
    targets = terminal_bench_targets(spec)
    filt = terminal_bench_filter(spec)
    pkg = package_name(spec)
    eps = ir_epsilon_pct(spec)

    base = measure_checkout(baseline_dir, package=pkg, bench_targets=targets,
                            measure_bin=bin_path, bench_filter=filt, runner=runner)
    cand = measure_checkout(candidate_dir, package=pkg, bench_targets=targets,
                            measure_bin=bin_path, bench_filter=filt, runner=runner)
    return judge_terminal(base, cand, epsilon_pct=eps)


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
