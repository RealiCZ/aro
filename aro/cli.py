"""cli — the single CLI surface (argparse subcommand registry).

Every subcommand's flags are declared HERE, once — replacing eight hand-rolled
`opt(argv, flag)` parsers where boolean flags and value flags used different
idioms and unknown flags were silently ignored. Modules keep their logic in
parameterized entry functions; this module owns parsing and dispatch.
"""
from __future__ import annotations

import argparse
import os


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aro",
        description="ARO: autonomous optimization loop; the deterministic judge is the moat.")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --- run -------------------------------------------------------------------
    r = sub.add_parser("run", help="run the per-target loop on a spec")
    r.add_argument("spec")
    r.add_argument("--rounds", type=int, default=None)
    r.add_argument("--aa-runs", type=int, default=None, dest="aa_runs")
    r.add_argument("--ab-pairs", type=int, default=None, dest="ab_pairs")
    r.add_argument("--out", default=None)
    r.add_argument("--generator", choices=("ralph", "agentic"), default=None)
    r.add_argument("--blind", action="store_true")
    r.add_argument("--no-read", action="store_true", dest="no_read")
    r.add_argument("--ignore-resume-failure", action="store_true",
                   dest="ignore_resume_failure")

    # --- plan ------------------------------------------------------------------
    pl = sub.add_parser("plan", help="free-form goal → validated 7-slot spec")
    pl.add_argument("goal")
    pl.add_argument("repo")
    pl.add_argument("--name", default=None)
    pl.add_argument("--crate", default=None)
    pl.add_argument("--baseline-ref", default="HEAD", dest="baseline_ref")
    pl.add_argument("--out", default=None)

    # --- sweep -----------------------------------------------------------------
    s = sub.add_parser("sweep", help="frontier map (L1) / unattended meta-loop (--attempt)")
    s.add_argument("spec")
    s.add_argument("--out", default=None, help="write the Markdown report here")
    s.add_argument("--min-pct", type=float, default=1.5, dest="min_pct")
    s.add_argument("--top", type=int, default=40)
    s.add_argument("--attempt", action="store_true")
    s.add_argument("--diverge", action="store_true")
    s.add_argument("--critic", action="store_true")
    s.add_argument("--max-attempts", type=int, default=None, dest="max_attempts")
    s.add_argument("--rounds-per-fn", type=int, default=None, dest="rounds_per_fn")
    s.add_argument("--max-tries-per-fn", type=int, default=0, dest="max_tries_per_fn")
    s.add_argument("--dry-rounds", type=int, default=None, dest="dry_rounds")
    s.add_argument("--fanout", type=int, default=None)
    s.add_argument("--gen-concurrency", type=int, default=8, dest="gen_concurrency")
    s.add_argument("--out-dir", default=None, dest="out_dir")
    s.add_argument("--prescreen", action="store_true", default=None)
    s.add_argument("--no-prescreen", action="store_false", dest="prescreen")
    s.add_argument("--exhaustive", action="store_true", default=None)
    s.add_argument("--no-exhaustive", action="store_false", dest="exhaustive")
    s.add_argument("--probe-factory", action="store_true", default=None,
                   dest="probe_factory")
    s.add_argument("--no-probe-factory", action="store_false", dest="probe_factory")
    s.add_argument("--workloads", type=int, default=0, metavar="N",
                   help="L4b campaign: after the base frontier exhausts, author + "
                        "qualify up to N synthetic workload variants (wins tagged "
                        "synthetic-workload, never auto-mergeable)")

    # --- tree / manifest / serve -------------------------------------------------
    t = sub.add_parser("tree", help="(re)render decision-tree.html + tree.json")
    t.add_argument("out_dir")
    t.add_argument("--out", default=None)

    m = sub.add_parser("manifest", help="final accepted edit-set → manifest.json")
    m.add_argument("out_dir")
    m.add_argument("--out", default=None)

    sv = sub.add_parser("serve", help="serve a run's report over HTTP, live-refreshing")
    sv.add_argument("out_dir")
    sv.add_argument("--port", type=int,
                    default=int(os.environ.get("ARO_SERVE_PORT", "8010")),
                    help="port (default $ARO_SERVE_PORT or 8010 — set the env var "
                         "once on a box instead of passing --port every run)")
    sv.add_argument("--every", type=int, default=30)
    sv.add_argument("--host", default="127.0.0.1",
                    help="bind address; pass 0.0.0.0 EXPLICITLY to expose on the network "
                         "(unauthenticated; firewall it or SSH-tunnel)")
    sv.add_argument("--no-watch", action="store_false", dest="watch")

    u = sub.add_parser("union", help="cross-campaign view over permtree ledgers "
                                     "(workload lanes, fn judgment matrix, open debt)")
    u.add_argument("specs", nargs="*",
                   help="ledger names (memory/permtree/<name>.jsonl); default: all")
    u.add_argument("--out", default=None, help="output HTML path (default union-report.html)")

    cov = sub.add_parser("coverage", help="dark-region report: workspace source NO "
                                          "registered workload executes (cargo-llvm-cov)")
    cov.add_argument("spec")
    cov.add_argument("--out", default=None,
                     help="artifact path (default targets/<spec>.coverage-gap.json, "
                          "where the workload factory's author prompt reads it)")

    rc = sub.add_parser("recheck", help="computed re-run signal: did the target repo's "
                                        "churn since the pinned baseline touch the "
                                        "editable regions?")
    rc.add_argument("spec")
    rc.add_argument("--ref", default="HEAD",
                    help="compare the baseline against this ref (default HEAD; "
                         "never fetches)")
    rc.add_argument("--json", action="store_true")

    c = sub.add_parser("clean", help="remove a spec's orphaned worktrees + target dirs "
                                     "(explicit, printed; never a background sweep)")
    c.add_argument("spec")
    c.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print what would be removed, remove nothing")
    c.add_argument("--registered", action="store_true",
                   help="also remove worktrees still registered with git "
                        "(after a crash, when NO campaign is running on this repo)")
    c.add_argument("--runs", default=None, metavar="DIR",
                   help="also remove run dirs under DIR not referenced by any "
                        "permanent ledger (referenced runs are the audit chain "
                        "behind recorded verdicts and are always kept)")

    # --- verify-patch / hotpath ---------------------------------------------------
    v = sub.add_parser("verify-patch", help="re-score a recorded patch through the full judge")
    v.add_argument("patch")
    v.add_argument("--spec", required=True)
    v.add_argument("--ab-pairs", type=int, default=4, dest="ab_pairs")
    v.add_argument("--aa-runs", type=int, default=3, dest="aa_runs")
    v.add_argument("--out", default=None)
    v.add_argument("--reuse-out", action="store_true", dest="reuse_out")

    h = sub.add_parser("hotpath", help="observe-only: profile the real hot path")
    h.add_argument("spec")

    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        from .__main__ import run_cli
        return run_cli(args)
    if args.cmd == "plan":
        from . import plan
        return plan.cli(args)
    if args.cmd == "sweep":
        from . import sweep
        return sweep.cli(args)
    if args.cmd == "tree":
        from . import tree
        return tree.cli(args)
    if args.cmd == "manifest":
        from . import manifest
        return manifest.cli(args)
    if args.cmd == "serve":
        from . import serve
        return serve.cli(args)
    if args.cmd == "union":
        from . import union
        return union.cli(args)
    if args.cmd == "clean":
        from . import clean
        return clean.cli(args)
    if args.cmd == "recheck":
        from . import recheck
        return recheck.cli(args)
    if args.cmd == "coverage":
        from . import coverage
        return coverage.cli(args)
    if args.cmd == "verify-patch":
        from . import verify
        return verify.cli(args)
    if args.cmd == "hotpath":
        return _hotpath(args)
    raise SystemExit(f"unknown command {args.cmd!r}")


def _hotpath(args) -> None:
    """Observe-only: build the spec's probe, measure the isolated kernel, profile it.
    (Absorbed from the root find_hotpath.py script.)"""
    from . import profile
    from . import spec as specmod
    from .stats import median
    from .target import SpecTarget

    sp = specmod.load(args.spec)
    b = sp.bench
    target = SpecTarget(sp)
    work = target.make_worktree("hotpath")
    try:
        print(f"building + measuring isolated kernel ({b['metric']}) ...")
        metrics = target.bench(work)
        samples = metrics.get(b["metric"]) or []
        if not samples:
            print("  (probe produced no samples; fix the probe before profiling)")
            return
        print(f"  {b['metric']}: median {median(samples):.1f} "
              f"(n={len(samples)}: {' '.join(f'{s:.0f}' for s in samples)})")
        binary = target.build_example(work)
        print("profiling (spin + sample) ...")
        funcs = profile.top_functions(binary, spin_secs=sp.profile.get("spin_secs", 8),
                                      sample_secs=sp.profile.get("sample_secs", 4))
        if not funcs:
            print("  (no profile: sampler unavailable or probe exited too fast)")
        for name, cnt, pct in funcs:
            print(f"  {pct:5.1f}%  {name}  ({cnt} samples)")
    finally:
        target.remove_worktree(work)
