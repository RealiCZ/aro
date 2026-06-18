"""ARO CLI — generic, spec-driven.

    python3 -m aro run targets/salt-committer.json [--rounds N] [--blind]
        [--aa-runs N] [--ab-pairs N] [--out DIR] [--no-read]

A target is a JSON spec in `targets/` (build/test/bench/regions/objectives +
goal + stop). The loop is the same for every target; only the spec changes —
that is how ARO generalizes. Generation is the agentic write-compile-fix loop
(live `claude`); the deterministic judge (`eval`/`stats`/`guard`) scores it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import spec as specmod
from .engine import run_backtest
from .events import EventLog
from .generator import AgenticGenerator, RalphGenerator
from .store import Memory
from .target import SpecTarget


def _opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def main(argv):
    if not argv or argv[0] != "run":
        raise SystemExit("usage: python3 -m aro run <spec.json> "
                         "[--rounds N] [--blind] [--generator ralph|agentic] "
                         "[--aa-runs N] [--ab-pairs N] [--out DIR] [--no-read]")
    spec = specmod.load(argv[1])
    if "--blind" in argv:
        spec.blind = True
    if "--no-read" in argv:
        spec.read_phase = False
    rounds = int(_opt(argv, "--rounds", spec.stop.max_rounds))
    aa_runs = int(_opt(argv, "--aa-runs", 2))
    ab_pairs = int(_opt(argv, "--ab-pairs", 4))
    out = Path(_opt(argv, "--out", f"./.aro-runs/{spec.name}"))
    out.mkdir(parents=True, exist_ok=True)
    gen_kind = _opt(argv, "--generator", spec.generator)

    print(f"=== ARO run: {spec.name} ===")
    print(f"repo={spec.repo} baseline={spec.baseline_ref} rounds={rounds} "
          f"generator={gen_kind} hint={'blind' if spec.blind else 'guided'} "
          f"read_phase={spec.read_phase}")
    print(f"goal: {spec.goal.direction} {spec.goal.metric}"
          + (f" -> {spec.goal.target}" if spec.goal.target is not None else " (open-ended)")
          + f"  | stop: max_rounds={spec.stop.max_rounds} dry_rounds={spec.stop.dry_rounds}\n")

    target = SpecTarget(spec)
    generator = (RalphGenerator(target.repo) if gen_kind == "ralph"
                 else AgenticGenerator(target))   # thin one-shot vs heavy write-compile-fix
    memory = Memory(out)
    events = EventLog(out / "events.jsonl", also_console=True)

    report = run_backtest(
        target, generator, memory,
        rounds=rounds, candidates_per_round=1,
        aa_runs=aa_runs, ab_pairs=ab_pairs, baseline_ref=spec.baseline_ref,
        events=events, goal=spec.goal, stop_dry_rounds=spec.stop.dry_rounds,
        read_phase=spec.read_phase,
    )
    # The run's machine-readable truth is events.jsonl — floors, every candidate's
    # verdict + Δ/CI/floor, pareto, elapsed, all structured. The human RUN-REPORT.md
    # is rendered FROM it by the `aro` skill's report flow (numbers copied verbatim),
    # not by Python: report prose stays out of code and can't launder a verdict.
    print(f"\n=== run finished: {len(report.outcomes)} candidate(s), "
          f"{len(report.pareto)} accepted, {report.elapsed_secs:.0f}s ===")
    print(f"truth source : {out / 'events.jsonl'}")
    print("render report: run the `aro` skill's report flow over that events.jsonl "
          "(skill/references/report-protocol.md)")


if __name__ == "__main__":
    main(sys.argv[1:])
