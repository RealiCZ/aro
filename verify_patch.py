"""Re-evaluate a previously-proposed patch as a seeded candidate, spec-driven.

Parse a `patches/<id>.txt` trace and re-run it through the full judge (for a
chosen target spec), to confirm a finding deterministically or re-test under
different settings.

    python3 verify_patch.py <patch-file> [--spec targets/X.json] [--out DIR] [--ab-pairs N]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from aro import spec as specmod
from aro.engine import run_backtest
from aro.events import EventLog
from aro.generator import PlannedGenerator
from aro import patchfile
from aro.store import Memory
from aro.target import SpecTarget


def parse_patch_file(path) -> list:
    """Parse a patches/<id>.txt file into Edits (format owned by aro.patchfile)."""
    return patchfile.parse(Path(path).read_text())


def _opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def main(argv):
    spec_path = _opt(argv, "--spec")
    if not argv or not spec_path:
        raise SystemExit("usage: python3 verify_patch.py <patch> --spec <spec.json> "
                         "[--ab-pairs N] [--aa-runs N] [--out DIR] [--reuse-out]")
    patch_file = argv[0]
    spec = specmod.load(spec_path)
    ab_pairs = int(_opt(argv, "--ab-pairs", 4))
    aa_runs = int(_opt(argv, "--aa-runs", 3))
    # A re-verify must be CLEAN: a shared out dir would load a prior run's Memory and
    # replay its accepted patches onto the baseline, contaminating the re-score. Default
    # to a fresh temp dir; `--out DIR` for an explicit location, `--reuse-out` to opt into
    # the resumable ./.aro-runs/verify (only when you actually want to continue it).
    out_arg = _opt(argv, "--out")
    if out_arg:
        out = Path(out_arg)
    elif "--reuse-out" in argv:
        out = Path("./.aro-runs/verify")
    else:
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="aro-verify-"))
    print(f"out: {out}")

    edits = parse_patch_file(patch_file)
    if not edits:
        raise SystemExit("no edits parsed from patch file")
    # Pre-check against the BASELINE_REF blob, not the working checkout: the judge
    # builds from baseline_ref, so a dirty tree or a checkout on a different commit
    # would make this count lie. Read each file at the frozen baseline via `git show`.
    sha = subprocess.run(["git", "-C", str(spec.repo), "rev-parse", spec.baseline_ref],
                         capture_output=True, text=True)
    base = sha.stdout.strip() if sha.returncode == 0 else spec.baseline_ref
    for e in edits:
        blob = subprocess.run(["git", "-C", str(spec.repo), "show", f"{base}:{e.path}"],
                              capture_output=True, text=True)
        if blob.returncode != 0:
            raise SystemExit(f"{e.path}: not found at baseline {spec.baseline_ref}")
        n = blob.stdout.count(e.search)
        print(f"edit {e.path}: search matches {n}x baseline ({spec.baseline_ref})")
        if n != 1:
            raise SystemExit("patch does not apply uniquely to the baseline")

    out.mkdir(parents=True, exist_ok=True)
    plan = [("verify", f"re-verify {Path(patch_file).name}", edits)]
    target = SpecTarget(spec)
    memory = Memory(out)
    events = EventLog(out / "events.jsonl", also_console=True)
    report = run_backtest(target, PlannedGenerator(plan), memory,
                          rounds=1, candidates_per_round=1,
                          aa_runs=aa_runs, ab_pairs=ab_pairs, baseline_ref=spec.baseline_ref,
                          events=events, bench_scales=spec.bench_scales)
    verdict = report.outcomes[0][1].verdict.value if report.outcomes else "(none)"
    print(f"\n>>> VERDICT: {verdict}")
    print(f"events: {out / 'events.jsonl'}  (render via the aro skill's report flow)")


if __name__ == "__main__":
    main(sys.argv[1:])
