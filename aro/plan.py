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
  6. WRITE targets/<name>.json (review it, then `aro run` on it).

The deterministic parts (detect / assemble / dry-run) are pure and import-testable;
only step 2 calls the selected LLM backend.
"""
from __future__ import annotations

import json
import re
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
            kinds = {k for t in p.get("targets", []) for k in t.get("kind", [])}
            crates.append({"name": p["name"],
                           "dir": str(Path(p["manifest_path"]).parent),
                           "kinds": sorted(kinds)})
    return crates


_LIB_KINDS = {"lib", "rlib", "dylib", "cdylib", "staticlib"}


def require_lib_target(crates: list, crate: str) -> None:
    """A probe is a cargo example doing `use <crate>::…` — that import needs a LIB
    target. A bin-only crate fails later with an unresolved-import compile error
    deep in a worktree; fail HERE with the actual fix instead."""
    info = next((c for c in crates if c["name"] == crate), None)
    if info and info.get("kinds") and not (_LIB_KINDS & set(info["kinds"])):
        raise SystemExit(
            f"crate `{crate}` has no library target (kinds: {', '.join(info['kinds'])}) — "
            f"a probe example cannot `use {crate.replace('-', '_')}::…`. Expose the hot "
            f"kernel via a [lib] target first (a thin src/lib.rs re-exporting it is enough)")


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
                  filled: dict, crate_rel: str = "") -> dict:
    """Compose the 7-slot spec dict from detection + the agent's judgment slots.
    `filled` carries: hot_path{file,fn}, metric, direction, sample_prefix,
    constraints{}, has_diff(bool). Default editable region is the WHOLE crate src
    (the guard supports directory prefixes): in attempt mode the region is
    retargeted per hot function anyway, and a single-file default only strangles
    the manual/seed path."""
    hp = filled.get("hot_path", {})
    oracle = {
        "build": ["cargo", "build", "--release", "-p", crate],
        "test": ["cargo", "test", "--release", "-p", crate],
    }
    if filled.get("has_diff", True):
        oracle["differential"] = {"pkg": crate, "probe": f"probes/{name}_diff.rs",
                                  "example": f"{name}_diff", "prefix": "DIFF"}
    crate_src = str(Path(crate_rel) / "src") if crate_rel and crate_rel != "." else "src"
    default_editable = [crate_src] if crate_rel else \
        ([hp["file"]] if hp.get("file") else [])
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
                       {"editable": default_editable,
                        "no_new_deps": True, "byte_identical": True},
        "run": {"generator": "agentic", "goal_target": None,
                "stop": {"max_rounds": 3, "dry_rounds": 2},
                "aa_runs": 2, "ab_pairs": 6, "timeout": 1800},
    }


# --- 4. dry-run (deterministic) -----------------------------------------------

def polarity_suspect(median_scale1, median_scaleN, scale: int) -> bool:
    """True when the bench sample looks like a COUNT rather than a per-op time.
    Per-op samples are ~scale-invariant (more reps per sample, same time each);
    count-like samples grow with the scale. With direction=minimize a count-like
    sample scores BACKWARDS (fewer ops = better), silently accepting slowdowns —
    the failure mode the old SPUN-prefix mega-evm sweep spec actually had. Pure,
    so the rule is unit-testable; threshold at half the scale factor."""
    if not median_scale1 or not median_scaleN or scale < 2:
        return False
    return (median_scaleN / median_scale1) > max(2.0, scale / 2.0)


def dry_run(spec) -> dict:
    """Build the baseline in a throwaway worktree and exercise the harness once:
    confirm it builds, the probe emits samples, samples are scale-SANE (per-op
    time, not a count: the polarity guard), tests pass, the differential probe
    emits a fingerprint, and the PROFILE arm sees a non-empty in-crate frontier
    (which requires the probe's spin mode: argv[1]=seconds). Returns a report
    dict; never raises (records the failure instead, so the slot dump can show
    exactly which leg is not yet sound)."""
    target = SpecTarget(spec)
    rep = {"build": None, "samples": None, "median": None, "polarity": None,
           "tests_pass": None, "diff_fingerprint": None,
           "profile_frames": None, "profile_top": None, "errors": []}
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
        med1 = None
        try:
            m = target.bench(work)
            s = m.get(spec.bench["metric"]) or []
            rep["samples"] = len(s)
            med1 = median(s) if s else None
            rep["median"] = round(med1, 2) if s else None
        except Exception as e:
            rep["errors"].append(f"bench: {e}")
        if med1:
            try:
                s8 = target.bench(work, scale=8).get(spec.bench["metric"]) or []
                if s8 and polarity_suspect(med1, median(s8), 8):
                    rep["polarity"] = "SUSPECT"
                    rep["errors"].append(
                        f"polarity: sample grows with ARO_BENCH_SCALE "
                        f"(x{median(s8) / med1:.1f} at scale 8) — looks like a COUNT, "
                        f"not a per-op time; under direction={spec.goal.direction} the "
                        f"judge would score it wrong. Emit per-op time samples")
                else:
                    rep["polarity"] = "ok" if s8 else None
            except Exception as e:
                rep["errors"].append(f"polarity: {e}")
        try:
            rep["tests_pass"] = target.test(work)
        except Exception as e:
            rep["errors"].append(f"test: {e}")
        if spec.differential:
            try:
                rep["diff_fingerprint"] = target.run_diff_probe(work, spec.differential)
            except Exception as e:
                rep["errors"].append(f"differential: {e}")
        try:
            _profile_leg(spec, target, work, rep)
        except Exception as e:
            rep["errors"].append(f"profile: {e}")
    finally:
        target.remove_worktree(work)
    return rep


def _profile_leg(spec, target, work, rep) -> None:
    """Dry-run leg 5: hotness is NOT taken on faith. Spin the probe, sample it, and
    require at least one in-crate frame — this catches both a probe without spin
    mode (exits too fast to sample: `spin_and_sample` returns nothing) and a
    workload that never touches the target's own code (all frames external)."""
    from .frontier import _workspace_tokens
    from .sweep import _sample_with_symbols
    from .symbols import classify_owner
    try:
        binary = target.build_example(work)
    except Exception as e:
        rep["errors"].append(f"profile: example build failed: {e}")
        return
    if not binary.exists():
        rep["errors"].append("profile: example binary not found after build")
        return
    rows = _sample_with_symbols(binary, spin=spec.profile.get("spin_secs", 8),
                                secs=spec.profile.get("sample_secs", 4), top=40,
                                our_token=_workspace_tokens(target, spec.bench["pkg"]))
    if not rows:
        rep["profile_frames"] = 0
        rep["errors"].append(
            "profile: no samples — the probe must support SPIN MODE (argv[1] = "
            "seconds: keep re-running the workload until the deadline), or it exits "
            "before the sampler can attach")
        return
    toks = _workspace_tokens(target, spec.bench["pkg"])
    ours = [(n, p) for n, p, sym in rows if classify_owner(sym, toks)[0] == "ours"]
    rep["profile_frames"] = len(ours)
    rep["profile_top"] = f"{ours[0][0]} {ours[0][1]:.1f}%" if ours else None
    if not ours:
        rep["errors"].append(
            "profile: samples exist but NO in-crate frames. Either the workload "
            "never reaches the target crate's own code, or the hot kernel is a "
            "small fn that rustc cross-crate-inlined into the probe (mark it "
            "#[inline(never)] to make it visible), or symbols are not demangling "
            "(see skill/references/new-box-checklist.md part 2)")


# --- 2. fill (the one agent call) ---------------------------------------------

def _make_worktree(repo: Path, baseline_ref: str) -> Path:
    from . import vcs
    parent = repo.parent / ".aro-worktrees"
    parent.mkdir(parents=True, exist_ok=True)
    wt = parent / f"plan-{time.monotonic_ns()}"
    try:
        vcs.worktree_add(repo, wt, baseline_ref)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"plan: git worktree add failed:\n{e}")
    # Mirror SpecTarget.make_worktree: a submodule-dependent repo (mega-evm's
    # forge-std) must build in the agent's throwaway worktree too, offline from
    # the main clone's object store.
    if (repo / ".gitmodules").exists():
        try:
            vcs.submodule_update(wt, timeout=600)
        except Exception:
            pass  # best-effort: the dry-run build will surface a real failure
    return wt


def _remove_worktree(repo: Path, wt: Path) -> None:
    from . import vcs
    vcs.worktree_remove(repo, wt)


def _fill_slots(goal: str, repo: Path, baseline_ref: str, crate: str, crate_rel: str,
                crates: list, name: str) -> dict:
    """Ask the agent to name the hot path, WRITE the two probes into aro-py/probes/,
    and emit the judgment slots as a JSON block. The agent runs in a THROWAWAY
    worktree of the target repo (cwd) — so it can build/verify freely and any
    git clean/restore it does only touches the disposable worktree, never the user's
    real working tree. Probe output is staged inside that writable sandbox; trusted
    Python copies it into aro-py before removing the worktree."""
    probe_path = REPO_ROOT / "probes" / f"{name}.rs"
    diff_path = REPO_ROOT / "probes" / f"{name}_diff.rs"
    # Delete any same-name probes from a previous `plan <name>` first, so the
    # `.exists()` checks below mean "the agent wrote it THIS round", not a stale leftover.
    probe_path.unlink(missing_ok=True)
    diff_path.unlink(missing_ok=True)
    crate_list = "\n".join(f"  - {c['name']}  ({c['dir']})" for c in crates)
    wt = _make_worktree(repo, baseline_ref)
    try:
        stage = wt / ".aro-plan-output"
        stage.mkdir(parents=True, exist_ok=True)
        staged_probe = stage / probe_path.name
        staged_diff = stage / diff_path.name
        prompt = prompts.load("plan", goal=goal, repo=str(wt), crate=crate,
                              crate_dir=crate_rel, crates=crate_list,
                              probe_path=str(staged_probe), diff_path=str(staged_diff),
                              prefix="BENCH")
        from .llm import LLMError, run_llm
        try:
            text, _toks, _ = run_llm(prompt, cwd=wt, timeout=1800,
                                     allow_write=True)
        except LLMError as e:
            raise SystemExit(f"plan agent failed: {e}")
        if staged_probe.exists():
            probe_path.write_text(staged_probe.read_text())
        if staged_diff.exists():
            diff_path.write_text(staged_diff.read_text())
    finally:
        _remove_worktree(repo, wt)
    m = re.search(r"\{.*\}", text, re.DOTALL)
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
    print(f"  sample polarity  : {rep.get('polarity') or '(not checked)'}")
    print(f"  tests passing    : {rep['tests_pass']}")
    print(f"  differential     : {rep['diff_fingerprint'] or '(none)'}")
    print(f"  profile (in-crate frames): {rep.get('profile_frames')}"
          + (f"  top: {rep['profile_top']}" if rep.get("profile_top") else ""))
    if rep["errors"]:
        print("  errors:")
        for e in rep["errors"]:
            print(f"    - {e}")
    # "Clean" requires NO errors and every leg the spec promises: build, samples,
    # a sane polarity, tests, a profilable in-crate frontier — and, when the spec
    # declares a differential, a non-empty fingerprint. (A leg that errored leaves
    # rep["errors"] non-empty, so the single `not rep["errors"]` gate covers all.)
    has_diff = bool(spec_dict.get("correctness_oracle", {}).get("differential"))
    ok = (not rep["errors"] and rep["build"] == "ok" and rep["samples"]
          and rep["tests_pass"] is not None and rep.get("profile_frames")
          and (rep["diff_fingerprint"] if has_diff else True))
    print("-" * 70)
    print("  VERDICT: " + ("dry-run clean — safe to run" if ok else
                           "dry-run INCOMPLETE — fix the errors above before running"))
    print("=" * 70)


def cli(args) -> None:
    goal = args.goal
    repo = Path(args.repo).expanduser().resolve()
    crates = detect_crates(repo)
    crate = pick_crate(crates, args.crate)
    require_lib_target(crates, crate)
    name = args.name or f"{crate}-opt"
    # Pin the baseline to a SHA at plan time: a symbolic ref ("HEAD", a branch)
    # re-resolves on every SpecTarget construction, so a moving HEAD mid-campaign
    # would pin different sub-tasks to different baselines.
    from . import vcs
    baseline_ref = vcs.rev_parse(repo, args.baseline_ref) or args.baseline_ref
    crate_dir = next(c["dir"] for c in crates if c["name"] == crate)
    try:
        crate_rel = str(Path(crate_dir).resolve().relative_to(repo))
    except ValueError:
        crate_rel = crate_dir  # crate outside the repo root (rare) — use absolute

    print(f"=== aro plan: {name} ===\nrepo={repo} crate={crate} ({crate_rel})\ngoal: {goal}\n")
    print("filling slots (agent reads the code + writes the probes, in a throwaway worktree) ...")
    filled = _fill_slots(goal, repo, baseline_ref, crate, crate_rel, crates, name)
    spec_dict = assemble_spec(name, repo, baseline_ref, crate, filled, crate_rel)

    print("dry-running the harness (build → probe → polarity → test → differential → profile) ...")
    spec = specmod.from_dict(spec_dict)
    try:
        specmod.validate_artifacts(spec)
    except specmod.SpecError as e:
        print(f"SPEC ERROR: {e}")
    rep = dry_run(spec)
    _dump(spec_dict, rep)

    out = Path(args.out or REPO_ROOT / "targets" / f"{name}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec_dict, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {out}")
    print(f"review it, then:  python3 -m aro run {out}")
