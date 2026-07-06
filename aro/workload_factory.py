"""workload_factory — L4b: self-authored WORKLOAD VARIANTS, held to a judge of
their own (design: docs/self-extending-search-design.md §3.2).

When a workload's frontier is exhausted, the factory has an agent author a new
deterministic workload variant (bench probe + its own differential oracle), then
puts it through DETERMINISTIC qualification gates before it may open a new
search axis:

  W1  determinism — the new oracle prints the SAME fingerprint on two runs;
  W2  oracle mutation test (decision W3: k seeded mutations, ALL must alarm) —
      an oracle that cannot detect a seeded behaviour change has no authority
      to certify byte-identical behaviour;
  W3  coverage increment — the new workload's profile must surface ≥1 in-crate
      function NOT already covered (a workload that adds no frontier mass is
      refused — this refusal chain is exactly what closes exhaustion boundary 3);
  W4  FREEZE — sha256 of both probes recorded (`workload_registered`) before any
      attempt runs under them.

Provenance (decision W2): wins found under a synthetic workload are regime
`synthetic-workload` — never auto-mergeable; a human confirms representativeness.
v1 scope (decision W4): variants change the INPUT DISTRIBUTION only.

Everything effectful is injectable (author runner, differential runner, profiler),
so the gate logic is unit-testable without cargo or a model.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from .llm import run_claude

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass
class WorkloadQualification:
    ok: bool
    name: str = ""
    probe_rel: str = ""
    diff_rel: str = ""
    probe_sha: str = ""
    diff_sha: str = ""
    new_fns: list = dataclasses.field(default_factory=list)
    mutations_alarmed: int = 0
    mutations_tried: int = 0
    reasons: list = dataclasses.field(default_factory=list)


def workload_paths(spec_name: str, wname: str):
    return (f"probes/{spec_name}-w-{wname}.rs", f"probes/{spec_name}-w-{wname}_diff.rs")


def workload_spec(spec, wname: str, probe_rel: str, diff_rel: str):
    """The parent spec re-pointed at the new workload's bench + oracle. The repo,
    build/test commands, constraints and knobs are inherited — only WHAT is
    measured and WHAT certifies behaviour change."""
    bench = dict(spec.bench)
    bench["probe"] = probe_rel
    bench["example"] = f"{spec.name}_w_{wname}".replace("-", "_")
    profile = dict(spec.profile)
    profile["example"] = bench["example"]
    diff = dict(spec.differential) if spec.differential else {}
    diff.update({"probe": diff_rel, "pkg": spec.bench["pkg"],
                 "example": bench["example"] + "_diff", "prefix": "DIFF"})
    return dataclasses.replace(spec, name=f"{spec.name}+{wname}", bench=bench,
                               profile=profile, differential=diff)


def _dark_context(spec, max_files: int = 10, fns_per_file: int = 8) -> str:
    """Authoring targets from the coverage-gap artifact (`aro coverage`), when
    one exists: named functions NO registered workload executes, grouped by
    file. Best-effort — with no artifact the author falls back to distribution
    variation, which the prompt says explicitly."""
    from . import coverage as covmod
    try:
        g = json.loads(covmod.gap_path(spec.name).read_text())
        fns = g.get("dark_fns") or []
    except Exception:
        return ("(no coverage-gap report; run `aro coverage <spec>` to get named "
                "dark-region targets — vary the input distribution instead)")
    if not fns:
        return ("(coverage-gap report: no dark functions — every workspace fn "
                "already executes; vary the input distribution instead)")
    by: dict = {}
    for d in fns:
        by.setdefault(d.get("file", "?"), []).append(d.get("fn", "?"))
    lines = [f"  {f}: " + ", ".join(sorted(set(ns))[:fns_per_file])
             + (" …" if len(set(ns)) > fns_per_file else "")
             for f, ns in sorted(by.items(), key=lambda kv: -len(kv[1]))[:max_files]]
    return ("Dark regions — functions NO registered workload executes; a variant "
            "that makes one of these run beats a pure distribution shift:\n"
            + "\n".join(lines))


def author(spec, wname: str, covered_fns, *, runner=None, timeout: int = 3600):
    """Have an agent WRITE the workload pair. Returns (probe_rel, diff_rel);
    raises on failure. `runner(prompt, worktree)` is injectable for tests."""
    from . import prompts
    from .target import SpecTarget

    probe_rel, diff_rel = workload_paths(spec.name, wname)
    probe_abs, diff_abs = REPO_ROOT / probe_rel, REPO_ROOT / diff_rel
    probe_abs.parent.mkdir(parents=True, exist_ok=True)
    probe_abs.unlink(missing_ok=True)
    diff_abs.unlink(missing_ok=True)

    prompt = prompts.load(
        "workload", pkg=spec.bench["pkg"],
        parent_probe=spec.bench.get("probe", "(none)"),
        covered_fns=", ".join(sorted(covered_fns)) or "(none yet)",
        dark_regions=_dark_context(spec),
        probe_path=str(probe_abs), diff_path=str(diff_abs))

    target = SpecTarget(spec)
    wt = target.make_worktree(f"wload-{wname}")
    try:
        if runner is not None:
            runner(prompt, wt)
        else:
            run_claude(prompt, cwd=wt, timeout=timeout, allow_write=True,
                       json_output=False)
    finally:
        target.remove_worktree(wt)
    for p, what in ((probe_abs, "bench probe"), (diff_abs, "differential probe")):
        if not p.exists():
            raise RuntimeError(f"workload agent did not write the {what} at {p}")
    return probe_rel, diff_rel


def qualify(spec, wname: str, probe_rel: str, diff_rel: str, *, covered_fns,
            k_mutations: int = 3, run_diff=None, mutate_diff=None,
            profile_fns=None, events=None) -> WorkloadQualification:
    """The workload-judge: gates W1–W4 (module docstring). Injectables:
      run_diff(wspec) -> fingerprint str | None     (one oracle run, unmutated)
      mutate_diff(wspec, k) -> (alarmed, tried)     (k seeded mutations, count alarms)
      profile_fns(wspec) -> [fn names] | None       (ranked in-crate hot fns)
    Defaults drive real cargo + the real profiler."""
    q = WorkloadQualification(ok=False, name=wname, probe_rel=probe_rel,
                              diff_rel=diff_rel)
    wspec = workload_spec(spec, wname, probe_rel, diff_rel)
    run_diff = run_diff or _real_run_diff
    mutate_diff = mutate_diff or _real_mutate_diff
    profile_fns = profile_fns or _real_profile_fns

    # W1 — determinism: two runs, one fingerprint.
    try:
        f1, f2 = run_diff(wspec), run_diff(wspec)
    except Exception as e:
        q.reasons.append(f"W1 oracle run failed: {str(e)[:200]}")
        return _register(q, events)
    if not f1 or f1 != f2:
        q.reasons.append(f"W1 non-deterministic oracle: {f1!r} vs {f2!r}")
        return _register(q, events)

    # W2 — mutation test: k seeded mutations, ALL compiling ones must alarm.
    try:
        alarmed, tried = mutate_diff(wspec, k_mutations)
    except Exception as e:
        q.reasons.append(f"W2 mutation test failed to run: {str(e)[:200]}")
        return _register(q, events)
    q.mutations_alarmed, q.mutations_tried = alarmed, tried
    if tried == 0:
        q.reasons.append("W2 unverifiable: no seeded mutation compiled")
        return _register(q, events)
    if alarmed < tried:
        q.reasons.append(f"W2 oracle too weak: only {alarmed}/{tried} seeded "
                         f"mutations alarmed (all must)")
        return _register(q, events)

    # W3 — coverage increment: ≥1 in-crate hot fn not already covered.
    fns = profile_fns(wspec)
    if fns is None:
        q.reasons.append("W3 unverifiable: workload profile produced no samples")
        return _register(q, events)
    q.new_fns = [f for f in fns if f not in set(covered_fns)]
    if not q.new_fns:
        q.reasons.append("W3 no frontier mass: every hot fn is already covered")
        return _register(q, events)

    # W4 — freeze both probes.
    q.probe_sha = hashlib.sha256((REPO_ROOT / probe_rel).read_bytes()).hexdigest()
    q.diff_sha = hashlib.sha256((REPO_ROOT / diff_rel).read_bytes()).hexdigest()
    q.ok = True
    return _register(q, events)


def _register(q: WorkloadQualification, events):
    if events is not None:
        events.emit("workload_registered", name=q.name, ok=q.ok,
                    probe=q.probe_rel, diff=q.diff_rel,
                    probe_sha=q.probe_sha or None, diff_sha=q.diff_sha or None,
                    new_fns=q.new_fns[:12],
                    mutations=f"{q.mutations_alarmed}/{q.mutations_tried}",
                    reasons=q.reasons)
    return q


def save(spec, q: WorkloadQualification) -> Path:
    """Persist a QUALIFIED workload as a spec fragment under
    targets/<spec>.workloads/ so later campaigns re-load it instead of
    re-authoring. Returns the path."""
    d = REPO_ROOT / "targets" / f"{spec.name}.workloads"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{q.name}.json"
    p.write_text(json.dumps({
        "name": q.name, "probe": q.probe_rel, "diff": q.diff_rel,
        "probe_sha": q.probe_sha, "diff_sha": q.diff_sha,
        "provenance": "synthetic-workload", "new_fns": q.new_fns,
    }, indent=1) + "\n")
    return p


def load_saved(spec) -> list:
    """Previously qualified workloads for this spec, oldest first."""
    d = REPO_ROOT / "targets" / f"{spec.name}.workloads"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            continue
    return out


# --- real backends -------------------------------------------------------------

def _real_run_diff(wspec):
    from .target import SpecTarget
    t = SpecTarget(wspec)
    work = t.make_worktree("wq-diff")
    try:
        return t.run_diff_probe(work, wspec.differential)
    finally:
        t.remove_worktree(work)


def _real_mutate_diff(wspec, k: int):
    """Seed up to k compiling mutations across the workload's hot in-crate fns and
    count how many the NEW oracle alarms on. Reuses the probe-factory mutator."""
    from . import frontier, sweep
    from .probe_factory import _mutate_fn_body
    from .target import SpecTarget

    t = SpecTarget(wspec)
    our = frontier._workspace_tokens(t, wspec.bench.get("pkg", wspec.name))
    ranked = sweep.profile_ranked(wspec, top=20, our_token=our)
    fns = [n for n, _pct, sym in ranked
           if frontier.classify_owner(sym, our)[0] == "ours"][:6]

    base_w = t.make_worktree("wq-mut-base")
    tried = alarmed = 0
    mut_w = None
    try:
        t.build(base_w)
        base_fp = t.run_diff_probe(base_w, wspec.differential)
        if not base_fp:
            raise RuntimeError("oracle produced no fingerprint on the baseline")
        for fn in fns:
            if tried >= k:
                break
            for rel in frontier._locate_fn(t, wspec.bench["pkg"], fn):
                src_p = Path(base_w) / rel
                if not src_p.exists():
                    continue
                src = src_p.read_text()
                for mutated in _mutate_fn_body(src, fn):
                    if tried >= k:
                        break
                    mut_w = mut_w or t.make_worktree("wq-mut")
                    (Path(mut_w) / rel).write_text(mutated)
                    try:
                        t.build(mut_w)
                    except Exception:
                        (Path(mut_w) / rel).write_text(src)
                        continue
                    tried += 1
                    fp = t.run_diff_probe(mut_w, wspec.differential)
                    if fp != base_fp:
                        alarmed += 1
                    (Path(mut_w) / rel).write_text(src)
                break
    finally:
        for w in (base_w, mut_w):
            if w is not None:
                t.remove_worktree(w)
    return alarmed, tried


def _real_profile_fns(wspec):
    from . import frontier, sweep
    from .target import SpecTarget
    our = frontier._workspace_tokens(SpecTarget(wspec),
                                     wspec.bench.get("pkg", wspec.name))
    rows = sweep.profile_ranked(wspec, top=40, our_token=our)
    if not rows:
        return None
    return [n for n, _pct, sym in rows
            if frontier.classify_owner(sym, our)[0] == "ours"]
