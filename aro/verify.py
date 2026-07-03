"""verify — re-evaluate a previously-proposed patch as a seeded candidate.

Parse a `patches/<id>.txt` trace and re-run it through the FULL judge (for a chosen
target spec), to confirm a finding deterministically or re-test under different
settings. Absorbed from the root verify_patch.py script (`aro verify-patch …`).
"""
from __future__ import annotations

from pathlib import Path

from . import patchfile, vcs
from . import spec as specmod
from .engine import run_backtest
from .events import EventLog
from .generator import PlannedGenerator
from .store import Memory
from .target import SpecTarget


def parse_patch_file(path) -> list:
    """Parse a patches/<id>.txt file into Edits (format owned by aro.patchfile)."""
    return patchfile.parse(Path(path).read_text())


def cli(args) -> None:
    spec = specmod.load(args.spec)
    # A re-verify must be CLEAN: a shared out dir would load a prior run's Memory and
    # replay its accepted patches onto the baseline, contaminating the re-score. Default
    # to a fresh temp dir; `--out DIR` for an explicit location, `--reuse-out` to opt into
    # the resumable ./.aro-runs/verify (only when you actually want to continue it).
    if args.out:
        out = Path(args.out)
    elif args.reuse_out:
        out = Path("./.aro-runs/verify")
    else:
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="aro-verify-"))
    print(f"out: {out}")

    edits = parse_patch_file(args.patch)
    if not edits:
        raise SystemExit("no edits parsed from patch file")
    # Pre-check against the BASELINE_REF blob, not the working checkout: the judge
    # builds from baseline_ref, so a dirty tree or a checkout on a different commit
    # would make this count lie. Read each file at the frozen baseline via `git show`.
    base = vcs.rev_parse(spec.repo, spec.baseline_ref) or spec.baseline_ref
    for e in edits:
        blob = vcs.show_blob(spec.repo, f"{base}:{e.path}")
        if blob is None:
            raise SystemExit(f"{e.path}: not found at baseline {spec.baseline_ref}")
        n = blob.count(e.search)
        print(f"edit {e.path}: search matches {n}x baseline ({spec.baseline_ref})")
        if n != 1:
            raise SystemExit("patch does not apply uniquely to the baseline")

    out.mkdir(parents=True, exist_ok=True)
    plan = [("verify", f"re-verify {Path(args.patch).name}", edits)]
    target = SpecTarget(spec)
    memory = Memory(out)
    events = EventLog(out / "events.jsonl", also_console=True)
    report = run_backtest(target, PlannedGenerator(plan), memory,
                          rounds=1, candidates_per_round=1,
                          aa_runs=args.aa_runs, ab_pairs=args.ab_pairs,
                          baseline_ref=spec.baseline_ref,
                          events=events, bench_scales=spec.bench_scales)
    verdict = report.outcomes[0][1].verdict.value if report.outcomes else "(none)"
    print(f"\n>>> VERDICT: {verdict}")
    print(f"events: {out / 'events.jsonl'}  (render via the aro skill's report flow)")
