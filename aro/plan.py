"""`aro plan` — turn a free-form goal into a validated 7-slot spec.

Semi-automatic, with a human slot-dump gate (the recommended shape):
  1. DETECT (deterministic): `cargo metadata` → crates + their build/test commands.
  2. FILL   (one agent call): read the goal + crate + code → name the hot_path,
            write the microbench + differential probes, emit the judgment slots
            (metric / direction / sample_prefix / constraints).
  3. ASSEMBLE: build the 7-slot spec from detect + the agent's slots.
  4. DRY-RUN (deterministic): a throwaway worktree → build → probe (samples?) →
            test (pass count?) → differential probe (fingerprint?). Each reported.
  5. SLOT DUMP: print the 7 slots + probe paths + dry-run results — the human gate.
  6. WRITE targets/<name>.json (review it, then `aro run`; or pass --run to chain).

The deterministic parts (detect / assemble / dry-run) are pure and import-testable;
only step 2 calls `claude`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import prompts, spec as specmod
from .stats import median
from .target import SpecTarget

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- 1. detect (deterministic) ------------------------------------------------

def detect_crates(repo: Path) -> list:
    """Workspace members as `[{name, dir}]` via `cargo metadata --no-deps`."""
    out = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=str(repo), capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError("cargo metadata failed:\n" + (out.stderr or "")[-800:])
    md = json.loads(out.stdout)
    members = set(md.get("workspace_members", []))
    crates = []
    for p in md.get("packages", []):
        if p.get("id") in members:
            crates.append({"name": p["name"],
                           "dir": str(Path(p["manifest_path"]).parent)})
    return crates


def pick_crate(crates: list, want: str = None) -> str:
    if want:
        if want not in {c["name"] for c in crates}:
            raise SystemExit(f"crate {want!r} not in workspace: "
                             + ", ".join(c["name"] for c in crates))
        return want
    if len(crates) == 1:
        return crates[0]["name"]
    raise SystemExit("multiple crates — pass --crate <name>: "
                     + ", ".join(c["name"] for c in crates))


# --- 3. assemble (deterministic, pure) ----------------------------------------

def assemble_spec(name: str, repo: Path, baseline_ref: str, crate: str,
                  filled: dict) -> dict:
    """Compose the 7-slot spec dict from detection + the agent's judgment slots.
    `filled` carries: hot_path{file,fn}, metric, direction, sample_prefix,
    constraints{}, has_diff(bool)."""
    hp = filled.get("hot_path", {})
    oracle = {
        "build": ["cargo", "build", "--release", "-p", crate],
        "test": ["cargo", "test", "--release", "-p", crate],
    }
    if filled.get("has_diff", True):
        oracle["differential"] = {"pkg": crate, "probe": f"probes/{name}_diff.rs",
                                  "example": f"{name}_diff", "prefix": "DIFF"}
    return {
        "name": name,
        "target_repo": {"path": str(repo), "baseline_ref": baseline_ref},
        "hot_path": {"file": hp.get("file"), "fn": hp.get("fn")},
        "metric": filled.get("metric", "ns_per_call"),
        "direction": filled.get("direction", "minimize"),
        "benchmark_probe": {
            "pkg": crate, "probe": f"probes/{name}.rs", "example": name,
            "sample_prefix": filled.get("sample_prefix", "BENCH"),
            "profile": {"spin_secs": 8, "sample_secs": 4},
        },
        "correctness_oracle": oracle,
        "constraints": filled.get("constraints", {}) or
                       {"editable": [hp["file"]] if hp.get("file") else [],
                        "no_new_deps": True, "byte_identical": True},
        "run": {"generator": "agentic", "goal_target": None,
                "stop": {"max_rounds": 3, "dry_rounds": 2},
                "aa_runs": 2, "ab_pairs": 6, "timeout": 1800},
    }


# --- 4. dry-run (deterministic) -----------------------------------------------

def dry_run(spec) -> dict:
    """Build the baseline in a throwaway worktree and exercise the harness once:
    confirm it builds, the probe emits samples, tests pass, and the differential
    probe emits a fingerprint. Returns a report dict; never raises (records the
    failure instead, so the slot dump can show exactly which leg is not yet sound)."""
    target = SpecTarget(spec)
    rep = {"build": None, "samples": None, "median": None,
           "tests_pass": None, "diff_fingerprint": None, "errors": []}
    try:
        work = target.make_worktree("plan-dryrun")
    except Exception as e:
        rep["errors"].append(f"worktree: {e}")
        return rep
    try:
        try:
            target.build(work); rep["build"] = "ok"
        except Exception as e:
            rep["build"] = "FAILED"; rep["errors"].append(f"build: {e}"); return rep
        try:
            m = target.bench(work)
            s = m.get(spec.bench["metric"]) or []
            rep["samples"] = len(s)
            rep["median"] = round(median(s), 2) if s else None
        except Exception as e:
            rep["errors"].append(f"bench: {e}")
        try:
            rep["tests_pass"] = target.test(work)
        except Exception as e:
            rep["errors"].append(f"test: {e}")
        if spec.differential:
            try:
                rep["diff_fingerprint"] = target._run_diff_probe(work, spec.differential)
            except Exception as e:
                rep["errors"].append(f"differential: {e}")
    finally:
        target.remove_worktree(work)
    return rep


# --- 2. fill (the one agent call) ---------------------------------------------

def _make_worktree(repo: Path, baseline_ref: str) -> Path:
    parent = repo.parent / ".aro-worktrees"
    parent.mkdir(parents=True, exist_ok=True)
    wt = parent / f"plan-{time.monotonic_ns()}"
    out = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach",
                          str(wt), baseline_ref], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("plan: git worktree add failed:\n" + (out.stderr or "")[-500:])
    return wt


def _remove_worktree(repo: Path, wt: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                   capture_output=True, text=True)
    shutil.rmtree(wt, ignore_errors=True)


def _fill_slots(goal: str, repo: Path, baseline_ref: str, crate: str, crate_rel: str,
                crates: list, name: str) -> dict:
    """Ask the agent to name the hot path, WRITE the two probes into aro-py/probes/,
    and emit the judgment slots as a JSON block. The agent runs in a THROWAWAY
    worktree of the target repo (cwd) — so it can build/verify freely and any
    git clean/restore it does only touches the disposable worktree, never the user's
    real working tree. The probes are written to absolute aro-py paths, so they
    survive the worktree's removal."""
    probe_path = REPO_ROOT / "probes" / f"{name}.rs"
    diff_path = REPO_ROOT / "probes" / f"{name}_diff.rs"
    # Delete any same-name probes from a previous `plan <name>` first, so the
    # `.exists()` checks below mean "the agent wrote it THIS round", not a stale leftover.
    probe_path.unlink(missing_ok=True)
    diff_path.unlink(missing_ok=True)
    crate_list = "\n".join(f"  - {c['name']}  ({c['dir']})" for c in crates)
    wt = _make_worktree(repo, baseline_ref)
    try:
        prompt = prompts.load("plan", goal=goal, repo=str(wt), crate=crate,
                              crate_dir=crate_rel, crates=crate_list,
                              probe_path=str(probe_path), diff_path=str(diff_path),
                              prefix="BENCH")
        out = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "-p", prompt],
            cwd=str(wt), capture_output=True, text=True, timeout=1800)
    finally:
        _remove_worktree(repo, wt)
    if out.returncode != 0:
        raise SystemExit("plan agent failed:\n" + (out.stderr or out.stdout)[-800:])
    m = re.search(r"\{.*\}", out.stdout, re.DOTALL)
    if not m:
        raise SystemExit("plan agent returned no JSON slot block")
    filled = json.loads(m.group(0))
    filled["has_diff"] = diff_path.exists()
    if not probe_path.exists():
        raise SystemExit(f"plan agent did not write the probe at {probe_path}")
    return filled


# --- orchestration ------------------------------------------------------------

def _dump(spec_dict: dict, rep: dict) -> None:
    print("\n" + "=" * 70)
    print("SLOT DUMP — review before running")
    print("=" * 70)
    print(json.dumps(spec_dict, indent=2, ensure_ascii=False))
    print("-" * 70)
    print("DRY-RUN:")
    print(f"  build            : {rep['build']}")
    print(f"  probe samples    : {rep['samples']}  (median {rep['median']})")
    print(f"  tests passing    : {rep['tests_pass']}")
    print(f"  differential     : {rep['diff_fingerprint'] or '(none)'}")
    if rep["errors"]:
        print("  errors:")
        for e in rep["errors"]:
            print(f"    - {e}")
    # "Clean" requires NO errors and every leg the spec promises: build, samples,
    # tests — and, when the spec declares a differential, a non-empty fingerprint.
    # (A differential that errored leaves rep["errors"] non-empty AND no fingerprint.)
    has_diff = bool(spec_dict.get("correctness_oracle", {}).get("differential"))
    ok = (not rep["errors"] and rep["build"] == "ok" and rep["samples"]
          and rep["tests_pass"] is not None
          and (rep["diff_fingerprint"] if has_diff else True))
    print("-" * 70)
    print("  VERDICT: " + ("dry-run clean — safe to run" if ok else
                           "dry-run INCOMPLETE — fix the errors above before running"))
    print("=" * 70)


def main(argv) -> None:
    if len(argv) < 2:
        raise SystemExit('usage: python3 -m aro plan "<goal>" <repo> '
                         "[--name N] [--crate C] [--out targets/N.json] [--run]")
    goal, repo_arg = argv[0], argv[1]
    repo = Path(repo_arg).expanduser().resolve()

    def opt(flag, default=None):
        return argv[argv.index(flag) + 1] if flag in argv else default

    crates = detect_crates(repo)
    crate = pick_crate(crates, opt("--crate"))
    name = opt("--name") or f"{crate}-opt"
    baseline_ref = opt("--baseline-ref", "HEAD")
    crate_dir = next(c["dir"] for c in crates if c["name"] == crate)
    try:
        crate_rel = str(Path(crate_dir).resolve().relative_to(repo))
    except ValueError:
        crate_rel = crate_dir  # crate outside the repo root (rare) — use absolute

    print(f"=== aro plan: {name} ===\nrepo={repo} crate={crate} ({crate_rel})\ngoal: {goal}\n")
    print("filling slots (agent reads the code + writes the probes, in a throwaway worktree) ...")
    filled = _fill_slots(goal, repo, baseline_ref, crate, crate_rel, crates, name)
    spec_dict = assemble_spec(name, repo, baseline_ref, crate, filled)

    print("dry-running the harness (build → probe → test → differential) ...")
    spec = specmod.from_dict(spec_dict)
    rep = dry_run(spec)
    _dump(spec_dict, rep)

    out = Path(opt("--out") or REPO_ROOT / "targets" / f"{name}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec_dict, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {out}")
    print(f"review it, then:  python3 -m aro run {out}")
