"""probe_factory — L4a: self-authored ISOLATION MICRO-BENCHES, held to a judge of
their own (design: docs/self-extending-search-design.md §3.1).

When a node is noise-limited (a consistent directional effect the parent workload's
bench can't resolve above its floor), the factory has an agent author a per-function
micro-bench, then puts that probe through DETERMINISTIC qualification gates before it
may judge anything:

  Q1  builds & emits parseable `BENCH …` samples;
  Q2  A/A floor beats the parent's (measurement power actually improved);
  Q3  relevance — the target fn owns ≥ `relevance_min` of the probe's self-time
      (profiler-verified; an unprofilable probe FAILS, honestly);
  Q4  scale-aware — ARO_BENCH_SCALE actually multiplies the work;
  Q5  FREEZE — sha256 of the probe file is recorded (`probe_registered`) BEFORE any
      candidate generation for the node, so a probe can never be tuned to flatter a
      specific patch (temporal separation, design §2).

The three moat rules the factory must never break: the patch-writing agent and the
probe-writing agent are separate calls that never see each other's output; the probe
only ever replaces Gate 2 (measurement) — Gate 1 correctness stays on the PARENT
differential; and a micro-proven win still needs a parent-workload non-regression
check before it folds (wired in sweep.attempt).

Everything effectful is injectable (author runner, bench, profiler), so the gate
logic is unit-testable without cargo or a model.
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

from . import eval as evalmod
from .llm import run_claude
from .stats import median

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass
class Qualification:
    """The probe-judge's verdict on one authored probe."""
    ok: bool
    probe_path: str = ""
    sha256: str = ""
    floor_pct: float = float("nan")       # micro-bench A/A floor (primary metric)
    parent_floor_pct: float = float("nan")
    relevance_pct: float = float("nan")   # target fn's share of probe self-time
    scale_ratio: float = float("nan")     # median(t@scale2)/median(t@scale1)
    reasons: list = dataclasses.field(default_factory=list)


def micro_spec(spec, fn: str, probe_rel: str):
    """The node's spec with Gate 2 swapped to the authored micro-bench. Gate 1
    (build/test/differential) is untouched — correctness stays on the PARENT oracle.
    The PROFILE example must follow the bench example: Q3's relevance check samples
    the binary named by profile.example — leaving it at the parent's name would
    profile a binary that is never built in the micro worktree (every probe would
    be wrongly rejected as unprofilable)."""
    ex = _example_name(spec.name, fn)
    bench = dict(spec.bench)
    bench["probe"] = probe_rel
    bench["example"] = ex
    profile = dict(spec.profile)
    profile["example"] = ex
    return dataclasses.replace(spec, bench=bench, profile=profile)


def probe_rel_path(spec_name: str, fn: str) -> str:
    return f"probes/{spec_name}-{_slug(fn)}-micro.rs"


def _example_name(spec_name: str, fn: str) -> str:
    return f"{_slug(spec_name)}_{_slug(fn)}_micro"


def _slug(s: str) -> str:
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in s)[:48]


def author(spec, fn: str, files: list, *, runner=None, timeout: int = 1800) -> str:
    """Have an agent WRITE the micro-bench probe file (repo-relative path returned).
    The agent runs in a throwaway worktree of the target repo; only the probe file
    (written to an absolute aro-py path) survives. Raises LLMError/RuntimeError on
    failure — the caller records and moves on. `runner` is injectable for tests."""
    from . import prompts
    from .target import SpecTarget

    rel = probe_rel_path(spec.name, fn)
    probe_path = REPO_ROOT / rel
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    probe_path.unlink(missing_ok=True)   # "exists" below must mean "authored NOW"

    prompt = prompts.load(
        "probe", fn=fn, files=", ".join(files), pkg=spec.bench["pkg"],
        parent_probe=spec.bench.get("probe", "(none)"),
        probe_path=str(probe_path), example=_example_name(spec.name, fn))

    target = SpecTarget(spec)
    wt = target.make_worktree(f"probe-{_slug(fn)}")
    try:
        if runner is not None:
            runner(prompt, wt)
        else:
            run_claude(prompt, cwd=wt, timeout=timeout, allow_write=True,
                       json_output=False)
    finally:
        target.remove_worktree(wt)
    if not probe_path.exists():
        raise RuntimeError(f"probe agent did not write {probe_path}")
    return rel


def qualify(spec, fn: str, probe_rel: str, *, parent_floors, objectives,
            aa_runs: int = 2, relevance_min: float = 60.0,
            floor_gain_max: float = 0.5, bench=None, profile_shares=None,
            events=None) -> Qualification:
    """Run the deterministic qualification gates Q1–Q5 (module docstring) on an
    authored probe. `bench(spec, scale) -> Metrics` and
    `profile_shares(spec) -> {leaf_name: pct}|None` are injectable for tests;
    the defaults drive real cargo + the real profiler."""
    q = Qualification(ok=False, probe_path=probe_rel)
    mspec = micro_spec(spec, fn, probe_rel)
    metric = spec.bench["metric"]
    q.parent_floor_pct = parent_floors.floor(metric)
    owned_bench = None
    if bench is None:
        bench = owned_bench = _RealBench(mspec)   # ONE worktree/build for all gate runs
    profile_shares = profile_shares or _real_profile_shares
    try:
        return _qualify_gates(q, mspec, fn, probe_rel, metric, objectives, aa_runs,
                              relevance_min, floor_gain_max, bench, profile_shares,
                              events)
    finally:
        if owned_bench is not None:
            owned_bench.close()


def _qualify_gates(q, mspec, fn, probe_rel, metric, objectives, aa_runs,
                   relevance_min, floor_gain_max, bench, profile_shares, events):

    # Q1+Q2 — build/emit + A/A floor must meaningfully beat the parent's.
    try:
        floors = _calibrate(mspec, aa_runs, objectives, bench)
    except Exception as e:
        q.reasons.append(f"Q1 build/bench failed: {str(e)[:200]}")
        return _register(q, events, fn)
    q.floor_pct = floors.floor(metric)
    # calibrate_floors clamps every floor to a 0.5% minimum, so "at the clamp" IS
    # maximum measurable power — accept it even when the parent floor is already
    # tight enough that a strict fractional gain is arithmetically impossible.
    if not (q.floor_pct < q.parent_floor_pct * floor_gain_max or q.floor_pct <= 0.5):
        q.reasons.append(
            f"Q2 floor {q.floor_pct:.3f}% did not beat parent "
            f"{q.parent_floor_pct:.3f}% by {floor_gain_max:.0%}")
        return _register(q, events, fn)

    # Q3 — relevance: the target fn must own the probe's self-time.
    shares = profile_shares(mspec)
    if not shares:
        q.reasons.append("Q3 relevance unverifiable: profiler produced no samples "
                         "— an unprofilable probe cannot be certified")
        return _register(q, events, fn)
    q.relevance_pct = shares.get(fn, 0.0)
    if q.relevance_pct < relevance_min:
        q.reasons.append(f"Q3 relevance {q.relevance_pct:.0f}% < {relevance_min:.0f}% "
                         f"(probe measures something else)")
        return _register(q, events, fn)

    # Q4 — scale-awareness: doubling ARO_BENCH_SCALE ~doubles per-sample time.
    try:
        t1 = median(bench(mspec, 1).get(metric) or [])
        t2 = median(bench(mspec, 2).get(metric) or [])
        q.scale_ratio = (t2 / t1) if t1 else float("nan")
    except Exception as e:
        q.reasons.append(f"Q4 scale bench failed: {str(e)[:160]}")
        return _register(q, events, fn)
    # NOTE: a per-call (ns/call) probe keeps per-sample time ~constant under scale
    # while averaging more work — both shapes are scale-aware. What ISN'T is a probe
    # whose ratio explodes or collapses (ignores scale AND has unstable timing).
    if not (0.6 <= q.scale_ratio <= 3.5):
        q.reasons.append(f"Q4 scale ratio {q.scale_ratio:.2f} outside [0.6, 3.5] "
                         f"(probe ignores ARO_BENCH_SCALE or is unstable)")
        return _register(q, events, fn)

    # Q5 — freeze: hash the probe BEFORE any candidate generation for this node.
    q.sha256 = hashlib.sha256((REPO_ROOT / probe_rel).read_bytes()).hexdigest()
    q.ok = True
    return _register(q, events, fn)


def _register(q: Qualification, events, fn: str) -> Qualification:
    if events is not None:
        events.emit("probe_registered", fn=fn, ok=q.ok, path=q.probe_path,
                    sha256=q.sha256 or None,
                    floor_pct=_num(q.floor_pct), parent_floor_pct=_num(q.parent_floor_pct),
                    relevance_pct=_num(q.relevance_pct), scale_ratio=_num(q.scale_ratio),
                    reasons=q.reasons)
    return q


def _num(x):
    return round(x, 3) if isinstance(x, (int, float)) and x == x else None


def _calibrate(mspec, aa_runs, objectives, bench):
    """A/A floors for the micro-spec through an injectable bench callable."""
    class _T:
        def bench(self, _work, scale=1):
            return bench(mspec, scale)
    return evalmod.calibrate_floors(_T(), None, aa_runs, objectives)


# --- parent-oracle coverage (the design's fragile-assumption check) ---------------

_MUTATIONS = (("==", "!="), ("<", "<="), (">", ">="), ("^", "|"),
              ("wrapping_add", "wrapping_sub"), ("rotate_left", "rotate_right"),
              ("+", "-"), ("*", "+"))


def _find_outside_strings(text: str, needle: str) -> int:
    """First index of `needle` that is not inside a Rust string/char literal (simple
    quote state machine; escapes handled). -1 when only literal-internal matches."""
    in_str = in_chr = esc = False
    i = 0
    while i < len(text):
        c = text[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif in_str:
            in_str = c != '"'
        elif in_chr:
            in_chr = c != "'"
        elif c == '"':
            in_str = True
        elif c == "'" and i + 2 < len(text) and text[i + 2] == "'":
            in_chr = True   # a char literal like 'x' (lifetimes have no closing ')
        elif text.startswith(needle, i):
            return i
        i += 1
    return -1


def _mutate_fn_body(src: str, fn: str):
    """Yield cheap seeded mutations of `fn`'s body: flip a comparison / an
    off-by-one-ish operator swap (first occurrence OUTSIDE string literals each).
    Textual + brace-matched — enough to produce compiling behaviour changes on most
    real functions; the caller tries EVERY variant until one alarms."""
    import re as _re
    m = _re.search(r"\bfn\s+" + _re.escape(fn) + r"\b", src)
    if not m:
        return
    start = src.find("{", m.end())
    if start < 0:
        return
    depth, i = 0, start
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = src[start:i]
    for a, b in _MUTATIONS:
        j = _find_outside_strings(body, a)
        if j >= 0:
            yield src[:start] + body[:j] + b + body[j + len(a):] + src[i:]


def parent_differential_covers(spec, fn: str, files: list, *, events=None):
    """Does the PARENT differential actually constrain `fn`'s behaviour? Seed one
    compiling mutation into the function; the parent differential must ALARM
    (fingerprints differ). Returns True (covered) / False (NOT covered — the
    'byte-identical' claim is weak for this fn) / None (unverifiable: no seeded
    mutation compiled). The L4a fragile assumption made checkable
    (docs/self-extending-search-design.md §5)."""
    from .target import SpecTarget
    t = SpecTarget(spec)
    base_w = mut_w = None
    verdict = None
    try:
        base_w = t.make_worktree("cov-base")
        t.build(base_w)
        for rel in files:
            src_p = Path(base_w) / rel
            if not src_p.exists():
                continue
            src = src_p.read_text()
            for mutated in _mutate_fn_body(src, fn):
                mut_w = mut_w or t.make_worktree("cov-mut")
                (Path(mut_w) / rel).write_text(mutated)
                try:
                    t.build(mut_w)
                except Exception:
                    (Path(mut_w) / rel).write_text(src)   # restore for the next try
                    continue
                try:
                    identical = t.differential(mut_w, base_w)
                except Exception:
                    identical = None
                if identical is False:        # one alarm = coverage proven
                    verdict = True
                    break
                # compiled but did NOT alarm — keep trying other mutations before
                # concluding not-covered (one degenerate mutation must not decide)
                verdict = False
                (Path(mut_w) / rel).write_text(src)
            if verdict is True:
                break
    except Exception:
        verdict = None
    finally:
        for w in (base_w, mut_w):
            if w is not None:
                t.remove_worktree(w)
    if events is not None:
        events.emit("parent_coverage", fn=fn, covered=verdict)
    return verdict


# --- real (cargo / profiler) backends ------------------------------------------

class _RealBench:
    """Qualification bench backend: ONE frozen worktree (built on first use) serving
    every gate run — Q1/Q2's A/A pairs and Q4's scale probes measure the same binary
    state, and we don't pay a fresh checkout+build per call."""

    def __init__(self, mspec):
        from .target import SpecTarget
        self._t = SpecTarget(mspec)
        self._work = None

    def __call__(self, _mspec, scale: int = 1):
        if self._work is None:
            self._work = self._t.make_worktree("probe-qual")
        return self._t.bench(self._work, scale)

    def close(self):
        if self._work is not None:
            self._t.remove_worktree(self._work)
            self._work = None


def _real_profile_shares(mspec):
    """{leaf_fn_name: self-time %} of the micro-probe binary, via the sweep
    profiler machinery. None when no samples could be taken."""
    from . import sweep as sweepmod
    rows = sweepmod.profile_ranked(mspec, top=40)
    if not rows:
        return None
    return {name: pct for name, pct, _sym in rows}
