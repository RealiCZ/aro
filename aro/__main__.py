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


def _opt(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


def main(argv):
    if argv and argv[0] == "plan":
        from . import plan
        return plan.main(argv[1:])
    if argv and argv[0] == "sweep":
        from . import sweep
        return sweep.main(argv[1:])
    if argv and argv[0] == "chart":
        from . import chart
        return chart.main(argv[1:])
    if argv and argv[0] == "tree":
        from . import tree
        return tree.main(argv[1:])
    if argv and argv[0] == "serve":
        from . import serve
        return serve.main(argv[1:])
    if not argv or argv[0] != "run":
        raise SystemExit(
            'usage: python3 -m aro plan "<goal>" <repo> [--name N] [--crate C] [--out F]\n'
            "       python3 -m aro sweep <spec.json> [--out report.md] [--min-pct P] [--top N]\n"
            "       python3 -m aro sweep <spec.json> --attempt [--max-attempts N] "
            "[--rounds-per-fn N] [--out-dir DIR]\n"
            "       python3 -m aro serve <out-dir> [--port 8010] [--every 30] [--no-watch]\n"
            "       python3 -m aro run <spec.json> "
            "[--rounds N] [--blind] [--generator ralph|agentic] "
            "[--aa-runs N] [--ab-pairs N] [--out DIR] [--no-read] "
            "[--ignore-resume-failure]")
    spec = specmod.load(argv[1])
    if "--blind" in argv:
        spec.blind = True
    if "--no-read" in argv:
        spec.read_phase = False
    rounds = int(_opt(argv, "--rounds", spec.stop.max_rounds))
    aa_runs = int(_opt(argv, "--aa-runs", spec.aa_runs))
    ab_pairs = int(_opt(argv, "--ab-pairs", spec.ab_pairs))
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
        ignore_resume_failure=("--ignore-resume-failure" in argv),
        bench_scales=spec.bench_scales,
    )
    # The run's machine-readable truth is events.jsonl — floors, every candidate's
    # verdict + Δ/CI/floor, pareto, elapsed, all structured. The human RUN-REPORT.md
    # is rendered FROM it by the `aro` skill's report flow (numbers copied verbatim),
    # not by Python: report prose stays out of code and can't launder a verdict.
    # Record each candidate as a durable cross-run lesson (memory/lessons.jsonl),
    # so future runs — any target — don't re-derive known dead ends or regressions.
    from . import lessons
    minz = {o["metric"]: o.get("minimize", True) for o in spec.objectives}
    # Improvement is direction-aware: for a minimize metric a more-negative Δ is
    # better, for a maximize metric a more-positive Δ is better. Record the Δ of the
    # objective that improved most in its own direction (min(d) is wrong for maximize).
    def _improvement(d):
        return -d.delta_pct if minz.get(d.metric, True) else d.delta_pct
    for cand, o in report.outcomes:
        best_d = max(o.deltas, key=_improvement, default=None)
        best = best_d.delta_pct if best_d is not None else None
        lessons.append(spec.name, cand.hypothesis, o.verdict.value, best,
                       o.notes[-1] if o.notes else "")

    print(f"\n=== run finished: {len(report.outcomes)} candidate(s), "
          f"{len(report.pareto)} accepted, {report.elapsed_secs:.0f}s ===")
    print(f"truth source : {out / 'events.jsonl'}")
    print("render report: run the `aro` skill's report flow over that events.jsonl "
          "(skill/references/report-protocol.md)")


if __name__ == "__main__":
    main(sys.argv[1:])
