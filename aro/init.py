"""`aro init` — flag-driven target onboarding scaffolder.

Inspect a Rust target repo (stdlib-only TOML-ish parse of package name /
workspace members — no cargo, no new deps), then write a minimal 7-slot spec
under `targets/<name>.json` plus two probe templates under `probes/`. Prints a
numbered checklist of what remains manual. Non-interactive (agents run it).

Limitation of the hand-parser: only top-level `[package] name = "..."` and
`[workspace] members = [...]` (single- or multi-line string arrays) are read.
Features, path deps, inherited workspace package tables, and non-string member
entries are ignored — pass `--package` when auto-detect is wrong.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

from .spec import REPO_ROOT, from_dict

# Metric / direction match mega-evm-v2 (and the field defaults in spec.py).
_DEFAULT_METRIC = "ns_per_call"
_DEFAULT_DIRECTION = "minimize"
_ICOUNT_EPS = 0.1


# --- Cargo.toml inspection (file reads only; never spawns) --------------------

def _strip_comment(line: str) -> str:
    """Drop `#` comments outside double-quoted strings (good enough for manifests)."""
    out, in_str, esc = [], False, False
    for ch in line:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\" and in_str:
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if ch == "#" and not in_str:
            break
        out.append(ch)
    return "".join(out)


def _parse_string(token: str) -> Optional[str]:
    token = token.strip()
    m = re.fullmatch(r'"([^"]*)"', token)
    return m.group(1) if m else None


def inspect_cargo_toml(repo: Path) -> dict:
    """Read `<repo>/Cargo.toml` → `{package_name, members, is_workspace}`.

    `package_name` is the root `[package] name` when present (None for a pure
    virtual workspace). `members` is the list of workspace member path globs
    as written (not expanded). Empty members + a package name ⇒ single crate.
    """
    manifest = Path(repo).resolve() / "Cargo.toml"
    if not manifest.is_file():
        raise SystemExit(f"aro init: no Cargo.toml at {manifest}")
    text = manifest.read_text(encoding="utf-8", errors="replace")
    section = None  # "package" | "workspace" | other
    package_name = None
    members: list = []
    collecting_members = False
    member_buf: list = []

    def _flush_members_line(s: str) -> None:
        nonlocal collecting_members
        # Pull every "..." token; allow trailing commas / brackets.
        for m in re.finditer(r'"([^"]*)"', s):
            member_buf.append(m.group(1))
        if "]" in s:
            members.extend(member_buf)
            member_buf.clear()
            collecting_members = False

    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            # Flush a half-open members array if a new section starts.
            if collecting_members:
                members.extend(member_buf)
                member_buf.clear()
                collecting_members = False
            inner = line[1:-1].strip()
            if inner == "package":
                section = "package"
            elif inner == "workspace":
                section = "workspace"
            else:
                section = None
            continue
        if collecting_members:
            _flush_members_line(line)
            continue
        if section == "package":
            m = re.match(r"name\s*=\s*(.+)$", line)
            if m and package_name is None:
                package_name = _parse_string(m.group(1))
        elif section == "workspace":
            m = re.match(r"members\s*=\s*(.+)$", line)
            if m:
                rest = m.group(1).strip()
                if rest.startswith("["):
                    collecting_members = True
                    _flush_members_line(rest)
    if collecting_members:
        members.extend(member_buf)

    return {
        "package_name": package_name,
        "members": members,
        "is_workspace": bool(members),
    }


def _member_package_name(repo: Path, member_path: str) -> Optional[str]:
    """Best-effort `[package] name` of a workspace member path (no globs)."""
    if any(ch in member_path for ch in "*?[]"):
        return None
    p = Path(repo).resolve() / member_path / "Cargo.toml"
    if not p.is_file():
        return None
    info = inspect_cargo_toml(p.parent)
    return info.get("package_name")


def resolve_package(repo: Path, want: Optional[str]) -> tuple:
    """Pick `(package_name, src_rel)` for the target crate.

    `src_rel` is the package's `src/` directory relative to the repo root
    (e.g. `src` or `crates/foo/src`). Raises SystemExit(2) when a workspace
    needs `--package` or the requested package is missing.
    """
    info = inspect_cargo_toml(repo)
    members = info["members"]
    root_pkg = info["package_name"]

    # Map package name → member path (relative). Root package uses "".
    pkg_to_dir: dict = {}
    if root_pkg:
        pkg_to_dir[root_pkg] = ""
    for mem in members:
        name = _member_package_name(repo, mem)
        if name:
            pkg_to_dir[name] = mem

    if want:
        if want not in pkg_to_dir:
            # Workspace member path used as --package is also accepted.
            if want in members:
                name = _member_package_name(repo, want) or Path(want).name
                return name, f"{want.rstrip('/')}/src"
            known = ", ".join(sorted(pkg_to_dir)) or "(none found)"
            raise SystemExit(
                f"aro init: package {want!r} not found. Known packages: {known}")
        d = pkg_to_dir[want]
        src = "src" if not d else f"{d.rstrip('/')}/src"
        return want, src

    # No --package: single-package auto-pick; multi-package asks.
    if members and len(pkg_to_dir) != 1:
        # Prefer listing package names when resolved; fall back to member paths.
        labels = sorted(pkg_to_dir) if pkg_to_dir else sorted(members)
        print("aro init: workspace has multiple members — pass --package <name>:",
              file=sys.stderr)
        for lab in labels:
            print(f"  {lab}", file=sys.stderr)
        raise SystemExit(2)
    if len(pkg_to_dir) == 1:
        name, d = next(iter(pkg_to_dir.items()))
        src = "src" if not d else f"{d.rstrip('/')}/src"
        return name, src
    if root_pkg and not members:
        return root_pkg, "src"
    raise SystemExit(
        "aro init: could not detect a package name in Cargo.toml "
        "(hand-parser only reads [package] name / [workspace] members); "
        "pass --package explicitly")


def default_name(package: str) -> str:
    """Slug for targets/<name>.json: package name with underscores → hyphens."""
    return package.replace("_", "-")


# --- Spec + probe generation --------------------------------------------------

def probe_basenames(name: str) -> tuple:
    """`(bench_stem, diff_stem)` used as probe filenames and cargo example names."""
    return f"{name}-probe", f"{name}-diff"


def build_spec_dict(name: str, repo: Path, package: str, src_rel: str) -> dict:
    """Minimal authored spec that `TargetSpec.from_dict` accepts.

    Deliberately omits certification-tier fields (terminal harness, pinned_tools,
    control lanes, policy families) — the checklist names them as add-ons.
    """
    bench_stem, diff_stem = probe_basenames(name)
    return {
        "name": name,
        "target_repo": {"path": str(Path(repo).resolve())},
        "metric": _DEFAULT_METRIC,
        "direction": _DEFAULT_DIRECTION,
        # Placeholder: humans fill real hot_path.file / .fn. String-shaped TODO
        # would break from_dict (expects a mapping); keep a dict with a TODO file.
        "hot_path": {"file": f"TODO: {src_rel}"},
        "benchmark_probe": {
            "pkg": package,
            "probe": f"probes/{bench_stem}.rs",
            "example": bench_stem,
            "sample_prefix": "BENCH",
        },
        "correctness_oracle": {
            "build": ["cargo", "build", "--release", "-p", package],
            "test": ["cargo", "test", "--release", "-p", package, "--lib"],
            "differential": {
                "pkg": package,
                "probe": f"probes/{diff_stem}.rs",
                "example": diff_stem,
                "prefix": "DIFF",
            },
        },
        "constraints": {
            "editable": [src_rel],
            "no_new_deps": True,
            "byte_identical": True,
        },
        "icount_epsilon_pct": _ICOUNT_EPS,
    }


_BENCH_TEMPLATE = r'''//! ARO benchmark probe (generated by `aro init`).
//!
//! Stdout contract: print ONE line starting with `BENCH` followed by per-call
//! nanosecond samples, e.g. `BENCH 12.3 11.8 12.1 …`. Trailing labels are fine.
//! Optional spin mode (argv[1] = seconds) prints `SPUN <n>` for the profiler.
//!
//! Determinism rules:
//!   - no OS randomness, no wall-clock seeds
//!   - fixed inputs / fixed seeds only
//!   - fold every observable into black_box so the optimizer cannot elide work
//!   - honor ARO_BENCH_SCALE (multiply inner reps) so the judge can auto-tighten
//!
//! // TODO(aro-init): replace the placeholder workload with the real hot path
//! // via the crate's public API (`use <crate>::…`).

use std::hint::black_box;
use std::time::{Duration, Instant};

fn placeholder_work(x: u64) -> u64 {
    // TODO(aro-init): call the real hot function here.
    x.wrapping_mul(0x9E37_79B9_7F4A_7C15).wrapping_add(1)
}

fn main() {
    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);
    let reps = 1_000u64.saturating_mul(scale).max(1);
    let input: u64 = 0xDEAD_BEEF_CAFE_F00D;

    if let Some(secs) = std::env::args().nth(1).and_then(|s| s.parse::<u64>().ok()) {
        let deadline = Instant::now() + Duration::from_secs(secs);
        let mut spins = 0u64;
        let mut sink = input;
        while Instant::now() < deadline {
            for _ in 0..reps {
                sink = black_box(placeholder_work(black_box(sink)));
            }
            spins += 1;
        }
        let _ = black_box(sink);
        println!("SPUN {}", spins);
        return;
    }

    // Warmup (untimed).
    let mut sink = input;
    for _ in 0..50 {
        sink = black_box(placeholder_work(black_box(sink)));
    }

    let mut samples = Vec::with_capacity(5);
    for _ in 0..5 {
        let t = Instant::now();
        for _ in 0..reps {
            sink = black_box(placeholder_work(black_box(sink)));
        }
        samples.push(t.elapsed().as_nanos() as f64 / reps as f64);
    }
    let _ = black_box(sink);
    let line = samples
        .iter()
        .map(|s| format!("{:.1}", s))
        .collect::<Vec<_>>()
        .join(" ");
    println!("BENCH {}", line);
}
'''

_DIFF_TEMPLATE = r'''//! ARO differential probe (generated by `aro init`).
//!
//! Stdout contract: print ONE line `DIFF <hex>` — a fingerprint of many
//! deterministic inputs folded together. Baseline and candidate must match
//! byte-for-byte or the correctness gate fails before significance scoring.
//!
//! Determinism rules:
//!   - no OS randomness, no wall-clock seeds (fixed PRNG seed only)
//!   - fold EVERY observable output into the fingerprint
//!   - same corpus in baseline and candidate worktrees
//!
//! // TODO(aro-init): replace the placeholder with the real public API under test.

fn xorshift64(state: &mut u64) -> u64 {
    let mut x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *state = x;
    x
}

fn placeholder_work(x: u64) -> u64 {
    // TODO(aro-init): call the real hot function; fold its full output.
    x.wrapping_mul(0x9E37_79B9_7F4A_7C15).wrapping_add(1)
}

fn main() {
    let mut state: u64 = 0xDEAD_BEEF_CAFE_F00D;
    let mut fp: u64 = 0;
    for case in 0..64u64 {
        let x = xorshift64(&mut state).wrapping_add(case);
        let y = placeholder_work(x);
        fp = fp.rotate_left(7) ^ y;
    }
    println!("DIFF {:016x}", fp);
}
'''


def render_bench_probe() -> str:
    return _BENCH_TEMPLATE


def render_diff_probe() -> str:
    return _DIFF_TEMPLATE


def checklist_lines(name: str, package: str) -> list:
    """Numbered post-init checklist (what remains manual)."""
    bench_stem, diff_stem = probe_basenames(name)
    return [
        f"1. Fill the probe TODOs: probes/{bench_stem}.rs and probes/{diff_stem}.rs "
        f"(replace placeholder_work with the real {package} public API; keep the "
        f"BENCH / DIFF stdout contracts).",
        "2. Run host `aro selfcheck targets/" + name + ".json` "
        "(measurement health marker required by icount/terminal gates).",
        "3. (Certification tier, optional) Add criterion+codspeed harness, "
        "terminal_bench_targets, measure_bin, pinned_tools, and floors calibration "
        "via `aro terminal-calibrate`.",
        "4. (Optional) Policy fields: control_lanes, protected_row_families, "
        "tradeable_regression_cap_pct, protected_hysteresis.",
    ]


def _write_file(path: Path, text: str, *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(
            f"aro init: refusing to overwrite {path} (pass --force)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_init(
    repo: Path,
    *,
    name: Optional[str] = None,
    package: Optional[str] = None,
    force: bool = False,
    out_root: Optional[Path] = None,
    stdout=None,
) -> dict:
    """Scaffold targets/<name>.json + two probes. Returns paths written.

    `out_root` defaults to the aro-py checkout (REPO_ROOT). Tests inject a
    tempdir so the real tree is never dirtied. Never writes outside
    `<out_root>/targets/` and `<out_root>/probes/`.
    """
    out = stdout if stdout is not None else sys.stdout
    root = Path(out_root) if out_root is not None else REPO_ROOT
    root = root.resolve()
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise SystemExit(f"aro init: --repo is not a directory: {repo}")

    pkg, src_rel = resolve_package(repo, package)
    slug = name or default_name(pkg)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", slug):
        raise SystemExit(
            f"aro init: invalid --name {slug!r} "
            f"(use letters, digits, ._- ; start with alnum)")

    spec_dict = build_spec_dict(slug, repo, pkg, src_rel)
    # Fail closed: refuse to write a spec from_dict cannot load.
    from_dict(spec_dict)

    bench_stem, diff_stem = probe_basenames(slug)
    targets_dir = root / "targets"
    probes_dir = root / "probes"
    # Hard fence: only these two subdirs.
    for d in (targets_dir, probes_dir):
        d.resolve().relative_to(root)  # raises if somehow outside

    spec_path = targets_dir / f"{slug}.json"
    bench_path = probes_dir / f"{bench_stem}.rs"
    diff_path = probes_dir / f"{diff_stem}.rs"

    for p in (spec_path, bench_path, diff_path):
        if p.exists() and not force:
            raise SystemExit(
                f"aro init: refusing to overwrite {p} (pass --force)")

    _write_file(spec_path, json.dumps(spec_dict, indent=2) + "\n", force=force)
    _write_file(bench_path, render_bench_probe(), force=force)
    _write_file(diff_path, render_diff_probe(), force=force)

    print(f"wrote {spec_path}", file=out)
    print(f"wrote {bench_path}", file=out)
    print(f"wrote {diff_path}", file=out)
    print("", file=out)
    print("Next steps (manual):", file=out)
    for line in checklist_lines(slug, pkg):
        print(line, file=out)

    return {
        "name": slug,
        "package": pkg,
        "src_rel": src_rel,
        "spec": spec_path,
        "bench_probe": bench_path,
        "diff_probe": diff_path,
        "spec_dict": spec_dict,
    }


def cli(args) -> None:
    run_init(
        Path(args.repo),
        name=args.name,
        package=args.package,
        force=bool(args.force),
    )
