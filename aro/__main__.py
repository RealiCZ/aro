"""ARO CLI — generic, spec-driven.

    python3 -m aro run targets/<name>.json [--rounds N] [--blind]
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


def run_cli(args) -> None:
    spec = specmod.load(args.spec)
    if args.blind:
        spec.blind = True
    if args.no_read:
        spec.read_phase = False
    rounds = args.rounds if args.rounds is not None else spec.stop.max_rounds
    aa_runs = args.aa_runs if args.aa_runs is not None else spec.aa_runs
    ab_pairs = args.ab_pairs if args.ab_pairs is not None else spec.ab_pairs
    out = Path(args.out or f"./.aro-runs/{spec.name}")
    out.mkdir(parents=True, exist_ok=True)
    gen_kind = args.generator or spec.generator

    print(f"=== ARO run: {spec.name} ===")
    print(f"repo={spec.repo} baseline={spec.baseline_ref} rounds={rounds} "
          f"generator={gen_kind} hint={'blind' if spec.blind else 'guided'} "
          f"read_phase={spec.read_phase}")
    print(f"goal: {spec.goal.direction} {spec.goal.metric}"
          + (f" -> {spec.goal.target}" if spec.goal.target is not None else " (open-ended)")
          + f"  | stop: max_rounds={spec.stop.max_rounds} dry_rounds={spec.stop.dry_rounds}\n")

    target = SpecTarget(spec)
    generator = (RalphGenerator(target) if gen_kind == "ralph"
                 else AgenticGenerator(target))   # thin one-shot vs heavy write-compile-fix
    memory = Memory(out)
    events = EventLog(out / "events.jsonl", also_console=True)

    report = run_backtest(
        target, generator, memory,
        rounds=rounds, candidates_per_round=1,
        aa_runs=aa_runs, ab_pairs=ab_pairs, baseline_ref=spec.baseline_ref,
        events=events, goal=spec.goal, stop_dry_rounds=spec.stop.dry_rounds,
        read_phase=spec.read_phase,
        ignore_resume_failure=args.ignore_resume_failure,
        bench_scales=spec.bench_scales,
    )
    # The run's machine-readable truth is events.jsonl — floors, every candidate's
    # verdict + Δ/CI/floor, pareto, elapsed, all structured. The human RUN-REPORT.md
    # is rendered FROM it by the `aro` skill's report flow (numbers copied verbatim),
    # not by Python: report prose stays out of code and can't launder a verdict.
    # Record each candidate as a durable cross-run lesson (memory/lessons.jsonl),
    # so future runs — any target — don't re-derive known dead ends or regressions.
    from . import lessons
    from .types import best_improvement
    minz = {o["metric"]: o.get("minimize", True) for o in spec.objectives}
    # Record the Δ of the objective that improved most in its own direction
    # (rule: types.best_improvement — shared with the engine's fold ranking).
    from .attempt import _lesson_gated
    for cand, o in report.outcomes:
        b = best_improvement(o.deltas, minz)
        best = b[0].delta_pct if b else None
        lessons.append(spec.name, cand.hypothesis, o.verdict.value, best,
                       o.notes[-1] if o.notes else "", gated=_lesson_gated(o),
                       ir_delta_pct=getattr(o, "ir_delta_pct", None),
                       profile_fingerprint=getattr(o, "profile_fingerprint", None),
                       env_fingerprint=getattr(o, "env_fingerprint", None))

    print(f"\n=== run finished: {len(report.outcomes)} candidate(s), "
          f"{len(report.pareto)} accepted, {report.elapsed_secs:.0f}s ===")
    print(f"truth source : {out / 'events.jsonl'}")
    print("render report: run the `aro` skill's report flow over that events.jsonl "
          "(skill/references/report-protocol.md)")


def main(argv):
    """Back-compat entry (`python3 -m aro …`) — parsing now lives in aro/cli.py."""
    from .cli import main as cli_main
    cli_main(argv)


def cli_entry():
    """Console-script entry point (`aro …` once pip-installed)."""
    from .cli import main as cli_main
    cli_main(sys.argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
