"""Host measurement self-check: probe A/A, tool fingerprint, pin check, marker.

Turns "the environment can measure" from a manual discipline into a
machine-enforced precondition. Gates (icount / terminal / terminal-calibrate)
require a fresh marker written by `aro selfcheck` before measuring.

Empirical ground truth: probe-level same-binary A/A spread ≈ 0.004%. A healthy
host must reproduce that order of magnitude; the default threshold
(`selfcheck_probe_max_pct`, 0.05) is ~10× the empirical floor.

Spec: T9 brief + ARO_ICOUNT_GATE_PLAN host health.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Host-local (gitignored under .aro-runs/). Not institutional memory.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MARKER_MAX_AGE_DAYS = 14
DEFAULT_PROBE_MAX_PCT = 0.05  # ~10× empirical same-binary A/A ≈ 0.004%
SKIP_ENV = "ARO_SKIP_SELFCHECK"
MARKER_SUBDIR = "selfcheck"

# Process-level cache for tool version probing (gates call this often).
# Only SUCCESSFUL probes are cached — failure results are re-probed next call.
_VERSION_CACHE: Optional[dict] = None

# Module-level injection seam for hermetic tests (evaluate / run_terminal paths
# that cannot always thread a runner kwarg). Set via `set_version_runner`.
_VERSION_RUNNER_OVERRIDE: Optional[Callable] = None


def set_version_runner(runner: Optional[Callable]) -> None:
    """Install (or clear, with None) a process-wide version-probe runner.

    Used by hermetic tests so evaluate()/run_terminal() never spawn real
    subprocesses even when they do not thread an explicit runner kwarg.
    """
    global _VERSION_RUNNER_OVERRIDE
    _VERSION_RUNNER_OVERRIDE = runner


class SelfcheckError(Exception):
    """Hard failure of the selfcheck gate (missing marker, stale, pin, spread).

    Never a verdict — the operator must fix the host and re-run selfcheck.
    Same class of error as profile-fidelity / TerminalError.
    """


# --- tool versions + env fingerprint -----------------------------------------

def _run_version_cmd(cmd: list, *, timeout: float = 15.0) -> str:
    """Run a version command; return combined stdout/stderr text.

    Tolerates nonzero exit: `cargo codspeed --version` is known to exit 1
    while still printing its clap banner (a clap quirk). Missing binaries
    return empty string so the fingerprint records `unknown`.
    """
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    return text


def _first_version_token(text: str) -> str:
    """Extract a version-ish token from tool banner text.

    Prefers a semver / dotted version after a known tool name, then any
    `v?X.Y…` token. When no version-like token is present (e.g. cargo's
    `error: no such command` with a normal nonzero exit), returns
    ``"unknown"`` — never the raw error text.
    """
    if not text:
        return ""
    # Common: "codspeed 4.18.3", "valgrind-3.26.0.codspeed5", "rustc 1.80.0 …"
    m = re.search(
        r"(?:codspeed|cargo-codspeed|cargo\s+codspeed|valgrind|rustc)"
        r"[\s\-]*v?([\w.+\-]+)",
        text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"\bv?(\d+\.[\w.+\-]+)", text)
    if m:
        return m.group(1).strip()
    return "unknown"


def _probe_succeeded(versions: dict) -> bool:
    """True when at least one tool returned a real version token.

    Transient host failures (missing PATH, tools not installed yet) yield
    empty/unknown for every key — those must NOT poison the process cache.
    """
    for key in ("codspeed", "cargo-codspeed", "valgrind", "rustc"):
        val = (versions.get(key) or "").strip()
        if val and val != "unknown":
            return True
    return False


def probe_tool_versions(*, runner: Optional[Callable] = None,
                        use_cache: bool = True) -> dict:
    """Probe codspeed / cargo-codspeed / valgrind / rustc versions.

    Returns dict with keys: codspeed, cargo-codspeed, valgrind, rustc
    (version strings; empty → treated as unknown by the fingerprint).
    `runner(cmd) -> text` injects for hermetic tests. Process-cached on
    successful probes only (failure results are re-probed next call) unless
    `use_cache=False` or a custom runner is supplied. A module-level
    injectable (`set_version_runner`) is used when `runner` is None.
    """
    global _VERSION_CACHE
    effective = runner if runner is not None else _VERSION_RUNNER_OVERRIDE
    if use_cache and effective is None and _VERSION_CACHE is not None:
        return dict(_VERSION_CACHE)

    run = effective if effective is not None else (
        lambda cmd: _run_version_cmd(list(cmd)))

    raw = {
        "codspeed": run(["codspeed", "--version"]),
        "cargo-codspeed": run(["cargo", "codspeed", "--version"]),
        "valgrind": run(["valgrind", "--version"]),
        "rustc": run(["rustc", "-V"]),
    }
    out = {k: _first_version_token(v) for k, v in raw.items()}
    # Also keep raw banners for diagnostics (failure messages).
    out["_raw"] = raw
    # Cache only successful probes so a transient host failure is re-tried.
    if use_cache and effective is None and _probe_succeeded(out):
        _VERSION_CACHE = dict(out)
    return out


def clear_version_cache() -> None:
    """Drop the process-level version cache (tests / after tool upgrades)."""
    global _VERSION_CACHE
    _VERSION_CACHE = None


def env_fingerprint(versions: Optional[dict] = None, *,
                    runner: Optional[Callable] = None,
                    use_cache: bool = True) -> str:
    """Canonical fingerprint: `codspeed=<v>;cargo-codspeed=<v>;valgrind=<v>;rustc=<v>`.

    Missing / empty tool → `unknown`. Stable key order so marker comparisons
    are exact string equality.
    """
    v = versions if versions is not None else probe_tool_versions(
        runner=runner, use_cache=use_cache)
    parts = []
    for key in ("codspeed", "cargo-codspeed", "valgrind", "rustc"):
        val = (v.get(key) or "").strip() or "unknown"
        parts.append(f"{key}={val}")
    return ";".join(parts)


def format_versions_human(versions: dict) -> str:
    """Multi-line human dump of tool versions (for CLI / failure messages)."""
    lines = []
    for key in ("codspeed", "cargo-codspeed", "valgrind", "rustc"):
        val = (versions.get(key) or "").strip() or "unknown"
        lines.append(f"  {key}: {val}")
    return "\n".join(lines)


def _pin_matches(found: str, expected: str) -> bool:
    """Exact match after trim, or token-boundary prefix for build-tag suffixes.

    `3.26.0` matches `3.26.0` and `3.26.0.codspeed5`, but NOT `13.26.0`
    (substring containment would false-positive on the latter).
    """
    if found == expected:
        return True
    # Build-tag suffix: pin is a strict prefix of found followed by a non-digit
    # (e.g. `.codspeed5`). startswith already rejects mid-token matches like
    # pin `3.26.0` against found `13.26.0`.
    if found.startswith(expected) and len(found) > len(expected):
        return not found[len(expected)].isdigit()
    return False


def check_pinned_tools(versions: dict, pinned: dict) -> Optional[str]:
    """Return an error message when a pin mismatches; None if all match.

    `pinned` is the target JSON `pinned_tools` object, e.g.
    {"codspeed": "4.18.3", "cargo-codspeed": "5.0.1",
     "valgrind": "3.26.0.codspeed5"}. Keys absent from `pinned` are not checked.
    Matching is exact after trim, or token-boundary prefix (pin `3.26.0`
    matches found `3.26.0.codspeed5` but not `13.26.0`).
    """
    if not pinned:
        return None
    mismatches = []
    for key, expected in pinned.items():
        if expected is None or str(expected).strip() == "":
            continue
        exp = str(expected).strip()
        found = (versions.get(key) or "").strip() or "unknown"
        if _pin_matches(found, exp):
            continue
        mismatches.append(f"{key}: expected {exp!r}, found {found!r}")
    if not mismatches:
        return None
    return ("selfcheck pin mismatch — tool versions do not match "
            "target pinned_tools:\n  " + "\n  ".join(mismatches) +
            f"\ncurrent fingerprint: {env_fingerprint(versions)}")


# --- marker lifecycle --------------------------------------------------------

def runs_root() -> Path:
    """Host-local runs root (override via ARO_RUNS_ROOT for tests)."""
    env = os.environ.get("ARO_RUNS_ROOT")
    if env is not None and str(env).strip():
        return Path(str(env).strip())
    return REPO_ROOT / ".aro-runs"


def marker_path(spec_name: str) -> Path:
    return runs_root() / MARKER_SUBDIR / f"{spec_name}.json"


def write_marker(spec_name: str, *, env_fp: str, probe_spread_pct: float,
                 rounds: int = 2, path: Optional[Path] = None,
                 extra: Optional[dict] = None) -> Path:
    """Write `.aro-runs/selfcheck/<spec>.json` on a successful selfcheck."""
    dest = path if path is not None else marker_path(spec_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "passed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env_fingerprint": env_fp,
        "probe_spread_pct": float(probe_spread_pct),
        "rounds": int(rounds),
    }
    if extra:
        payload.update(extra)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return dest


def read_marker(spec_name: str, *, path: Optional[Path] = None
                ) -> Optional[dict]:
    """Load marker JSON or None when missing / unreadable."""
    dest = path if path is not None else marker_path(spec_name)
    if not dest.is_file():
        return None
    try:
        doc = json.loads(dest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _parse_iso_utc(s: str) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def marker_age_days(marker: dict, *, now: Optional[datetime] = None
                    ) -> Optional[float]:
    """Age of marker in days, or None if `passed_at` is missing/unparseable."""
    dt = _parse_iso_utc(marker.get("passed_at") or "")
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def validate_marker(marker: Optional[dict], *, current_fp: str,
                    max_age_days: float = DEFAULT_MARKER_MAX_AGE_DAYS,
                    now: Optional[datetime] = None,
                    spec_name: str = "<spec>") -> Optional[str]:
    """Return hard-error message if marker is missing/stale/mismatched; else None."""
    if marker is None:
        return (f"selfcheck: no marker for {spec_name!r} — run "
                f"`python3 -m aro selfcheck <spec>` first")
    age = marker_age_days(marker, now=now)
    if age is None:
        return (f"selfcheck: marker for {spec_name!r} has missing/invalid "
                f"passed_at — re-run `python3 -m aro selfcheck <spec>`")
    if age > float(max_age_days):
        return (f"selfcheck: marker for {spec_name!r} is {age:.1f} days old "
                f"(max {max_age_days:g}) — re-run "
                f"`python3 -m aro selfcheck <spec>` first")
    stored = (marker.get("env_fingerprint") or "").strip()
    cur = (current_fp or "").strip()
    if not stored:
        return (f"selfcheck: marker for {spec_name!r} lacks env_fingerprint "
                f"— re-run `python3 -m aro selfcheck <spec>`")
    if stored != cur:
        return (f"selfcheck: env_fingerprint mismatch for {spec_name!r}\n"
                f"  marker:  {stored}\n"
                f"  current: {cur}\n"
                f"tool versions changed since last selfcheck — re-run "
                f"`python3 -m aro selfcheck <spec>` first")
    return None


def skip_selfcheck_requested() -> bool:
    """True only for explicit truthy env values: `1` / `true` / `yes` (case-insensitive).

    Everything else — empty, `0`, `false`, `no`, typos, garbage — is off.
    """
    v = str(os.environ.get(SKIP_ENV, "") or "").strip().lower()
    return v in ("1", "true", "yes")


def warn_skip_selfcheck(*, via: str = "") -> None:
    """Loud stderr warning shared by env override and `skip_selfcheck=True`."""
    reason = via if via else f"{SKIP_ENV} is set"
    print(
        f"WARNING: {reason} — skipping selfcheck marker gate. "
        "Measurement health is NOT verified; floors/verdicts may be garbage. "
        "Unset the override and run `python3 -m aro selfcheck <spec>` "
        "as soon as possible.",
        file=sys.stderr,
    )


def require_selfcheck(spec, *, runner: Optional[Callable] = None,
                      use_cache: bool = True,
                      marker_path_override: Optional[Path] = None,
                      now: Optional[datetime] = None,
                      skip: bool = False) -> Optional[str]:
    """Gate precondition: valid selfcheck marker or skip override.

    Returns the current env_fingerprint when the check passes. Returns None
    when skipped (env override or ``skip=True``) — callers follow
    skip-when-absent and omit fingerprint fields rather than probing fresh.
    Raises SelfcheckError on missing/stale/mismatched marker.

    `ARO_SKIP_SELFCHECK=1` (or `true`/`yes`) and ``skip=True`` bypass with a
    loud stderr warning and short-circuit BEFORE any version probing so
    hermetic tests never spawn real subprocesses.
    """
    # Short-circuit BEFORE version probing — hermetic tests rely on this.
    if skip:
        warn_skip_selfcheck(via="skip_selfcheck=True")
        return None
    if skip_selfcheck_requested():
        warn_skip_selfcheck(via=f"{SKIP_ENV} is set")
        return None

    versions = probe_tool_versions(runner=runner, use_cache=use_cache)
    fp = env_fingerprint(versions)
    name = getattr(spec, "name", None) or "unknown"
    marker = read_marker(str(name), path=marker_path_override)
    err = validate_marker(marker, current_fp=fp, now=now, spec_name=str(name))
    if err:
        raise SelfcheckError(err)
    return fp


# --- probe A/A ---------------------------------------------------------------

def probe_max_pct(spec=None) -> float:
    """Max allowed same-binary probe A/A spread (%). Target field or default."""
    if spec is not None:
        v = getattr(spec, "selfcheck_probe_max_pct", None)
        if v is not None:
            return float(v)
        raw = getattr(spec, "raw", None) or {}
        if "selfcheck_probe_max_pct" in raw:
            return float(raw["selfcheck_probe_max_pct"])
    return DEFAULT_PROBE_MAX_PCT


def pinned_tools(spec=None) -> dict:
    if spec is None:
        return {}
    v = getattr(spec, "pinned_tools", None)
    if isinstance(v, dict) and v:
        return dict(v)
    raw = getattr(spec, "raw", None) or {}
    p = raw.get("pinned_tools")
    return dict(p) if isinstance(p, dict) else {}


def same_binary_spread_pct(ir_a: int, ir_b: int) -> float:
    """|Ir_a − Ir_b| / mean(Ir) * 100.

    Two zero Ir readings are a measurement error (probe produced no
    instructions), not a 0% pass — raise rather than report perfect agreement.
    """
    a, b = int(ir_a), int(ir_b)
    mean = (a + b) / 2.0
    if mean == 0:
        raise ValueError(
            "both Ir measurements unusable (zero mean) — measurement error, "
            "not a 0% pass")
    return abs(a - b) / mean * 100.0


@dataclass
class SelfcheckResult:
    ok: bool
    env_fingerprint: str
    probe_spread_pct: Optional[float] = None
    versions: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    marker_path: Optional[str] = None
    row_warnings: list = field(default_factory=list)


def run_probe_aa(target, work, *, scale: int = 1,
                 icount_fn: Optional[Callable] = None) -> tuple:
    """Build once, measure Ir twice on the same binary; return (ir1, ir2, spread%).

    Reuses `target.icount` (which rebuilds via build_example — cargo is
    incremental so the second call reuses the binary). `icount_fn` injects a
    hermetic double that returns successive ICountResult-like objects.
    """
    if icount_fn is not None:
        r1 = icount_fn(work, scale=scale)
        r2 = icount_fn(work, scale=scale)
    else:
        r1 = target.icount(work, scale=scale, cache_sim=False)
        r2 = target.icount(work, scale=scale, cache_sim=False)
    ir1 = int(getattr(r1, "ir", r1))
    ir2 = int(getattr(r2, "ir", r2))
    spread = same_binary_spread_pct(ir1, ir2)
    return ir1, ir2, spread


def check_row_set_integrity(measure_rows: dict, floors: dict
                            ) -> list:
    """Warn when calibrated floor rows are missing from measure output (or vice versa).

    Does NOT attempt row-level A/A (that is `terminal-calibrate`'s job). Returns
    a list of warning strings (empty when sets match or floors empty).
    """
    if not floors:
        return ["selfcheck --rows: no calibrated floors file — row-set check skipped"]
    mkeys = set(measure_rows or {})
    fkeys = set(floors or {})
    warns = []
    missing = sorted(fkeys - mkeys)
    extra = sorted(mkeys - fkeys)
    if missing:
        warns.append(
            f"selfcheck --rows: {len(missing)} calibrated floor row(s) missing "
            f"from measure output (first: {missing[:3]})")
    if extra:
        warns.append(
            f"selfcheck --rows: {len(extra)} measure row(s) have no calibrated "
            f"floor (first: {extra[:3]}) — floors may be stale vs current benches")
    if not missing and not extra:
        warns.append(
            f"selfcheck --rows: row-set OK ({len(mkeys)} rows match floors file)")
    return warns


def run_selfcheck(spec, *, target=None, work=None,
                  version_runner: Optional[Callable] = None,
                  icount_fn: Optional[Callable] = None,
                  rows: bool = False,
                  measure_rows: Optional[dict] = None,
                  floors: Optional[dict] = None,
                  marker_path_override: Optional[Path] = None,
                  make_worktree: bool = True) -> SelfcheckResult:
    """Run the full selfcheck pipeline; write marker on pass.

    When `target`/`work`/`icount_fn` are injected the probe A/A is hermetic.
    When only `spec` is given, constructs SpecTarget + worktree (production).
    """
    versions = probe_tool_versions(runner=version_runner, use_cache=False)
    fp = env_fingerprint(versions)
    notes = [f"env_fingerprint: {fp}", format_versions_human(versions)]

    pin_err = check_pinned_tools(versions, pinned_tools(spec))
    if pin_err:
        notes.append(pin_err)
        return SelfcheckResult(ok=False, env_fingerprint=fp,
                               versions=versions, notes=notes)

    # Probe A/A — core health signal. Own the worktree only when we create it.
    owned_work = None
    spread: Optional[float] = None
    ir1 = ir2 = 0
    row_warnings: list = []
    try:
        if icount_fn is None and target is None:
            from .target import SpecTarget
            target = SpecTarget(spec)
        if icount_fn is None and work is None and make_worktree and target is not None:
            owned_work = target.make_worktree("selfcheck")
            work = owned_work
        if icount_fn is None and work is None and target is None:
            raise SelfcheckError(
                "selfcheck: no target/worktree available for probe A/A")

        ir1, ir2, spread = run_probe_aa(
            target, work, icount_fn=icount_fn)

        max_pct = probe_max_pct(spec)
        notes.append(
            f"probe A/A: Ir={ir1} / {ir2}  spread={spread:.4f}%  "
            f"max={max_pct}%  rounds=2")
        if spread >= max_pct:
            notes.append(
                f"selfcheck FAIL: probe A/A spread {spread:.4f}% >= "
                f"{max_pct}% threshold — host measurement is noisy / broken "
                f"(healthy hosts see ~0.004%). Tools:\n"
                f"{format_versions_human(versions)}")
            return SelfcheckResult(
                ok=False, env_fingerprint=fp, probe_spread_pct=spread,
                versions=versions, notes=notes)

        if rows:
            # Row-set integrity only — not row-level A/A (terminal-calibrate's job).
            # Run while the worktree still exists so measure can reuse it.
            fl = floors
            if fl is None:
                try:
                    from . import terminal as tm
                    fl, _meta, _w = tm.load_floors(
                        getattr(spec, "name", None) or "unknown")
                except Exception as e:
                    fl = {}
                    row_warnings.append(
                        f"selfcheck --rows: floors load failed: {e}")
            mrows = measure_rows if measure_rows is not None else {}
            if measure_rows is None:
                try:
                    from . import terminal as tm
                    if tm.has_terminal_config(spec):
                        checkout = work
                        if checkout is None:
                            repo = (getattr(spec, "repo", None) or
                                    (getattr(spec, "raw", None) or
                                     {}).get("target_repo") or {})
                            if isinstance(repo, dict):
                                checkout = repo.get("path")
                        if checkout:
                            doc = tm.measure_checkout(
                                checkout,
                                package=tm.package_name(spec),
                                bench_targets=tm.terminal_bench_targets(spec),
                                measure_bin=tm.resolve_measure_bin(spec),
                                bench_filter=tm.terminal_bench_filter(spec),
                                timeout=tm.resolve_terminal_timeout(spec),
                            )
                            mrows = doc.rows
                except Exception as e:
                    row_warnings.append(
                        f"selfcheck --rows: measure failed ({e}); "
                        "row-set check skipped")
            row_warnings.extend(check_row_set_integrity(mrows, fl or {}))
            notes.extend(row_warnings)

        name = getattr(spec, "name", None) or "unknown"
        dest = write_marker(
            str(name), env_fp=fp, probe_spread_pct=spread, rounds=2,
            path=marker_path_override)
        notes.append(f"marker written → {dest}")
        return SelfcheckResult(
            ok=True, env_fingerprint=fp, probe_spread_pct=spread,
            versions=versions, notes=notes, marker_path=str(dest),
            row_warnings=row_warnings,
        )
    except Exception as e:
        notes.append(f"selfcheck probe A/A failed: {e}")
        return SelfcheckResult(ok=False, env_fingerprint=fp,
                               versions=versions, notes=notes,
                               probe_spread_pct=spread,
                               row_warnings=row_warnings)
    finally:
        if owned_work is not None and target is not None:
            try:
                target.remove_worktree(owned_work)
            except Exception:
                pass


# --- CLI ---------------------------------------------------------------------

def cli(args) -> None:
    """`aro selfcheck <spec> [--rows]` — host measurement health gate."""
    from . import spec as specmod

    try:
        sp = specmod.load(args.spec)
    except Exception:
        raw = json.loads(Path(args.spec).read_text())
        sp = specmod.from_dict(raw)

    rows = bool(getattr(args, "rows", False))
    print(f"selfcheck for {getattr(sp, 'name', '?')}:")
    print(f"  probe max spread: {probe_max_pct(sp)}%")
    pins = pinned_tools(sp)
    if pins:
        print(f"  pinned_tools:     {pins}")
    else:
        print("  pinned_tools:     (none — record-only, no pin enforcement)")
    print(f"  --rows:           {rows} "
          f"(row-set integrity vs floors; NOT row-level A/A — "
          f"that is terminal-calibrate)")
    print(f"  marker path:      {marker_path(getattr(sp, 'name', 'unknown'))}")

    result = run_selfcheck(sp, rows=rows)

    for n in result.notes:
        print(n)
    if result.ok:
        print(f"selfcheck PASS  spread={result.probe_spread_pct:.4f}%  "
              f"fp={result.env_fingerprint}")
        raise SystemExit(0)
    print("selfcheck FAIL", file=sys.stderr)
    raise SystemExit(1)
