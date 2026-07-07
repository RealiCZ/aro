"""`aro coverage` — the dark-region report: what NO registered workload executes.

The exhaustion proof's coverage boundary closes when the workload factory runs
dry, but "dry" only says no NEW frontier mass was reachable — it never says how
much of the crate the registered workloads execute at all. This measures it:
build every registered workload probe instrumented (cargo-llvm-cov), run them
all into ONE merged profile, and report the workspace source that never ran —
files ranked darkest-first, zero-count functions named. The dark list is the
honest footnote on any exhaustion claim, and it is the workload factory's
authoring TARGET list: a variant that lights a named dark region beats one that
merely shifts input distribution (the artifact is written where the factory's
author prompt picks it up).

External dependency: cargo-llvm-cov (+ the llvm-tools-preview component); the
command degrades to an actionable install message when missing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .symbols import _demangle_names

REPO_ROOT = Path(__file__).resolve().parent.parent


def have() -> bool:
    return shutil.which("cargo-llvm-cov") is not None


def gap_path(spec_name: str) -> Path:
    """Where the dark-region artifact lives — the workload factory reads it."""
    return REPO_ROOT / "targets" / f"{spec_name}.coverage-gap.json"


def registered_workloads(spec) -> list:
    """[(example, probe_rel)] to run: the base bench probe first, then every
    qualified factory workload saved under targets/<spec>.workloads."""
    from . import workload_factory as wfmod
    out = [(spec.bench["example"], spec.bench["probe"])]
    for w in wfmod.load_saved(spec):
        out.append((f"{spec.name}_w_{w['name']}".replace("-", "_"), w["probe"]))
    return out


def merged_export(spec, *, runner=None) -> dict:
    """Build + run every registered workload probe instrumented, in one fresh
    worktree, merging counts into a single profile (llvm-cov's no-report/report
    flow). Returns (parsed export JSON, worktree path prefix used)."""
    from .target import SpecTarget
    target = SpecTarget(spec)
    pkg = spec.bench["pkg"]
    wt = target.make_worktree("coverage")
    try:
        env = target.env_for(wt)
        env["ARO_BENCH_SCALE"] = "1"

        def _run(cmd):
            if runner is not None:
                return runner(cmd, wt, env)
            return subprocess.run(cmd, cwd=str(wt), env=env, capture_output=True,
                                  text=True, timeout=spec.timeout * 4)

        for ex, probe in registered_workloads(spec):
            dst = target.pkg_dir(wt, pkg) / "examples" / f"{ex}.rs"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text((REPO_ROOT / probe).read_text())
            r = _run(["cargo", "llvm-cov", "run", "--release", "--no-report",
                      "-p", pkg, "--example", ex])
            if r.returncode != 0:
                tail = "\n".join(((r.stderr or "") + (r.stdout or "")).splitlines()[-15:])
                raise RuntimeError(f"instrumented run of `{ex}` failed:\n{tail}")
        # --release must match the runs above: report searches the profile's
        # own object dir (llvm-cov-target/release)
        rep = _run(["cargo", "llvm-cov", "report", "--release", "--json"])
        if rep.returncode != 0:
            raise RuntimeError("cargo llvm-cov report failed:\n"
                               + "\n".join((rep.stderr or "").splitlines()[-15:]))
        return json.loads(rep.stdout), str(wt)
    finally:
        target.remove_worktree(wt)


def _rel_to(prefix: str):
    pfx = str(prefix).rstrip("/") + "/"

    def rel(f: str):
        f = f or ""
        if not f.startswith(pfx):
            return None                       # a dependency / registry file
        r = f[len(pfx):]
        if r.startswith("examples/") or "/examples/" in r:
            return None                       # the probes themselves
        return r
    return rel


def dark_regions(export: dict, work_prefix: str, our_token="") -> dict:
    """From one merged export: the workspace source no registered workload
    executed. Pure and testable. Files ranked darkest-first; every zero-count
    function named (demangled when a demangler is available)."""
    data = (export.get("data") or [{}])[0]
    rel = _rel_to(work_prefix)
    files = []
    for f in data.get("files") or []:
        r = rel(f.get("filename", ""))
        if r is None:
            continue
        s = f.get("summary") or {}
        fn = s.get("functions") or {}
        ln = s.get("lines") or {}
        files.append({"file": r, "functions": fn.get("count", 0),
                      "covered": fn.get("covered", 0),
                      "dark_fns": fn.get("count", 0) - fn.get("covered", 0),
                      "line_pct": round(ln.get("percent", 0.0), 1)})
    files.sort(key=lambda x: (-x["dark_fns"], x["file"]))

    dark = []
    for fn in data.get("functions") or []:
        if fn.get("count", 0) != 0:
            continue
        rels = [r for r in map(rel, fn.get("filenames") or []) if r]
        if not rels:
            continue
        dark.append({"symbol": fn.get("name", ""), "file": rels[0]})
    for d, name in zip(dark, _demangle_names([d["symbol"] for d in dark],
                                             our_token, "")):
        d["fn"] = name
    # closures/shims ({closure#0}, {vtable.shim}) are not authoring targets —
    # their enclosing fn already appears; the per-file counts still include them
    dark = [d for d in dark if not d["fn"].startswith("{")]
    dark.sort(key=lambda d: (d["file"], d["fn"]))
    total = sum(f["functions"] for f in files)
    covered = sum(f["covered"] for f in files)
    return {"files": files, "dark_fns": dark,
            "totals": {"functions": total, "covered": covered,
                       "dark": total - covered,
                       "covered_pct": round(100.0 * covered / total, 1) if total else None}}


def cli(args) -> None:
    from . import spec as specmod
    if not have():
        raise SystemExit(
            "cargo-llvm-cov not found — install it first:\n"
            "  cargo install cargo-llvm-cov\n"
            "  rustup component add llvm-tools-preview")
    sp = specmod.load(args.spec)
    pairs = registered_workloads(sp)
    print(f"coverage over {len(pairs)} registered workload(s): "
          + ", ".join(ex for ex, _ in pairs))
    export, prefix = merged_export(sp)
    from .frontier import _workspace_tokens
    from .target import SpecTarget
    g = dark_regions(export, prefix,
                     our_token=_workspace_tokens(SpecTarget(sp), sp.bench["pkg"]))
    out = Path(args.out or gap_path(sp.name))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(g, ensure_ascii=False, indent=1) + "\n")
    t = g["totals"]
    print(f"coverage-gap → {out}")
    print(f"  functions: {t['functions']} total · {t['covered']} executed · "
          f"{t['dark']} dark"
          + (f" ({t['covered_pct']}% covered)" if t["covered_pct"] is not None else ""))
    for f in g["files"][:10]:
        if f["dark_fns"]:
            print(f"  dark: {f['file']} — {f['dark_fns']}/{f['functions']} fn(s) "
                  f"never ran (line cov {f['line_pct']}%)")
    if not any(f["dark_fns"] for f in g["files"]):
        print("  no dark functions — the registered workloads light the whole crate")
