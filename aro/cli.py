"""cli — the single CLI surface (argparse subcommand registry).

Every subcommand's flags are declared HERE, once — replacing eight hand-rolled
`opt(argv, flag)` parsers where boolean flags and value flags used different
idioms and unknown flags were silently ignored. Modules keep their logic in
parameterized entry functions; this module owns parsing and dispatch.
"""
from __future__ import annotations

import argparse
import os
import sys

# Soft-deprecated top-level commands: still dispatch, one stderr warning each.
_DEPRECATED_CMDS = frozenset({
    "run", "plan", "union", "next", "coverage", "clean", "verify-patch", "hotpath",
})

# Top-level aliases that print a one-line note then dispatch to the canonical path.
_ALIAS_NOTES = {
    "recheck-debts": "note: 'aro recheck-debts' is now 'aro recheck debts' (alias kept)",
    "reverify": "note: 'aro reverify' is now 'aro recheck candidates' (alias kept)",
    "terminal-calibrate": (
        "note: 'aro terminal-calibrate' is now 'aro terminal --calibrate' (alias kept)"
    ),
}

_RECHECK_ACTIONS = frozenset({"staleness", "debts", "candidates"})


def _note(msg: str) -> None:
    print(msg, file=sys.stderr)


def _deprecated_warning(name: str) -> None:
    _note(
        f"warning: 'aro {name}' is deprecated "
        f"(unused in production; may be removed in a future release)"
    )


def _normalize_recheck_argv(argv: list[str]) -> tuple[list[str], str | None]:
    """Bare `aro recheck <spec>…` → `aro recheck staleness <spec>…` + notice.

    Nested subcommands are the canonical form; the old positional-spec form
    stays as a soft alias so scripts and `aro next` output keep working.
    """
    if not argv or argv[0] != "recheck":
        return argv, None
    if len(argv) == 1:
        # `aro recheck` with nothing → staleness (will fail on missing spec).
        return ["recheck", "staleness"], (
            "note: 'aro recheck' is now 'aro recheck staleness' (alias kept)"
        )
    second = argv[1]
    if second in _RECHECK_ACTIONS or second in ("-h", "--help"):
        return argv, None
    # Any other token (spec path, --ref, --json, …) is the old bare form.
    return ["recheck", "staleness", *argv[1:]], (
        "note: 'aro recheck' is now 'aro recheck staleness' (alias kept)"
    )


def _add_recheck_staleness_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec")
    p.add_argument("--ref", default="HEAD",
                   help="compare the baseline against this ref (default HEAD; "
                        "never fetches)")
    p.add_argument("--json", action="store_true")


def _add_recheck_debts_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="measure but do not append to permtree/lessons")
    p.add_argument("--list-only", action="store_true", dest="list_only",
                   help="list open debts + patch recoverability; measure nothing")
    p.add_argument("--runs-root", default=None, dest="runs_root",
                   help="optional root for resolving relative .aro-runs paths")


def _add_recheck_candidates_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--spec", required=True,
                   help="target JSON (current gates / differential probe)")
    p.add_argument("--out", required=True,
                   help="campaign run dir: reads manifest.json + aN/patches/, "
                        "writes reverify.json")
    p.add_argument("--orders", default=None,
                   help="comma-separated 1-based orders to gate "
                        "(e.g. 1,3,8); others still apply for compounding "
                        "and are marked skipped")
    p.add_argument(
        "--apply", action="store_true",
        help="stamp each manifest entry additively with "
             "\"reverify\": {verdict, failing_gate?} and force "
             "mergeable=false for every non-reverify-pass entry. "
             "NEVER sets mergeable=true — promotion stays a human decision.")


def _add_terminal_calibrate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("spec")
    p.add_argument("--checkout", required=True,
                   help="checkout / worktree to measure repeatedly (no rebuilds)")
    p.add_argument("--rounds", type=int, default=None,
                   help="measure rounds (default 4; must be >= 2)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print the measure command and destination; do not invoke")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aro",
        description="ARO: autonomous optimization loop; the deterministic judge is the moat.")
    sub = p.add_subparsers(dest="cmd", required=True)

    # --- run [deprecated] ------------------------------------------------------
    r = sub.add_parser("run", help="[deprecated] run the per-target loop on a spec")
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

    # --- plan [deprecated] -----------------------------------------------------
    pl = sub.add_parser("plan", help="[deprecated] free-form goal → validated 7-slot spec")
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
    m.add_argument("--spec", default=None,
                   help="target JSON; when it declares terminal_bench_targets, "
                        "mergeable also requires TERMINAL_CONFIRMED")
    m.add_argument("--terminal", default=None,
                   help="path to a terminal.json (from `aro terminal --out`); "
                        "also auto-loaded from <out_dir>/terminal.json when present")
    m.add_argument("--clear-quarantine", type=int, default=None, metavar="ORDER",
                   dest="clear_quarantine",
                   help="clear outlier quarantine for accepted entry ORDER "
                        "(1-based); requires --by and --evidence; loads existing "
                        "manifest.json, writes quarantine_audit, re-resolves mergeable")
    m.add_argument("--by", default=None,
                   help="who cleared the quarantine (required with --clear-quarantine)")
    m.add_argument("--evidence", default=None,
                   help="what was reviewed and why it passed "
                        "(required with --clear-quarantine)")
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

    u = sub.add_parser("union", help="[deprecated] cross-campaign view over permtree "
                                     "ledgers (workload lanes, fn judgment matrix, "
                                     "open debt)")
    u.add_argument("specs", nargs="*",
                   help="ledger names (memory/permtree/<name>.jsonl); default: all")
    u.add_argument("--out", default=None, help="output HTML path (default union-report.html)")

    nx = sub.add_parser("next", help="[deprecated] the next-action oracle: read all "
                                     "recorded state, print THE next action + why "
                                     "(the automation seam)")
    nx.add_argument("spec")
    nx.add_argument("--json", action="store_true")
    nx.add_argument("--mark", default=None, metavar="WHAT",
                    help="record operator-completed state the disk cannot infer "
                         "(harvested, interrupted)")

    cov = sub.add_parser("coverage", help="[deprecated] dark-region report: workspace "
                                          "source NO registered workload executes "
                                          "(cargo-llvm-cov)")
    cov.add_argument("spec")
    cov.add_argument("--out", default=None,
                     help="artifact path (default targets/<spec>.coverage-gap.json, "
                          "where the workload factory's author prompt reads it)")

    # --- recheck namespace (staleness / debts / candidates) -----------------------
    rc = sub.add_parser(
        "recheck",
        help="recheck family: staleness (baseline churn), debts (Ir re-adjudication), "
             "candidates (replay correctness gates on a frozen manifest)")
    rc_sub = rc.add_subparsers(dest="recheck_action", required=False)

    rcs = rc_sub.add_parser(
        "staleness",
        help="computed re-run signal: did the target repo's churn since the pinned "
             "baseline touch the editable regions?")
    _add_recheck_staleness_args(rcs)

    rcd = rc_sub.add_parser(
        "debts",
        help="Ir-gate re-adjudication of permtree open debts "
             "(noise-limited / no-attempt / …): recover stored "
             "patches and re-judge under instruction counts")
    _add_recheck_debts_args(rcd)

    rcc = rc_sub.add_parser(
        "candidates",
        help="re-adjudicate frozen manifest candidates through current "
             "correctness gates (build → test → optional test_full → "
             "differential). Replay compounds in manifest order.")
    _add_recheck_candidates_args(rcc)

    # Aliases for pre-namespace names (soft deprecation — still work).
    rd = sub.add_parser(
        "recheck-debts",
        help="[deprecated] alias of `aro recheck debts`")
    _add_recheck_debts_args(rd)

    # --- terminal (+ --calibrate) ----------------------------------------------
    tm = sub.add_parser(
        "terminal",
        help="pre-PR criterion Ir terminal gate: measure both "
             "worktrees via mega-bench-reporter measure "
             "--instructions and diff row-level Ir "
             "(or --rejudge / --list / --calibrate)")
    tm.add_argument("spec")
    tm.add_argument("--baseline", default=None,
                    help="baseline worktree (required unless --list/--rejudge/--calibrate)")
    tm.add_argument("--candidate", default=None,
                    help="candidate worktree (required unless --list/--rejudge/--calibrate)")
    tm.add_argument("--out", default=None,
                    help="write terminal.json (verdict + bench_ir_rows)")
    # Mode flags: measure (default) / list / rejudge / calibrate — exclusive.
    _tm_mode = tm.add_mutually_exclusive_group()
    _tm_mode.add_argument("--list", action="store_true",
                          help="print terminal config; do not measure (no binary needed)")
    _tm_mode.add_argument("--rejudge", default=None, metavar="PATH",
                          help="offline re-adjudication of an existing terminal.json "
                               "(uses --spec floors + control_lanes; writes "
                               "<PATH>.rejudged.json; never overwrites the input; "
                               "mutually exclusive with --baseline/--candidate/"
                               "--list/--calibrate)")
    _tm_mode.add_argument(
        "--calibrate", action="store_true",
        help="calibrate per-row terminal floors via repeated measure of one "
             "checkout (writes memory/floors/<spec>.json); requires --checkout; "
             "mutually exclusive with measure/--rejudge/--list")
    tm.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="with --calibrate: print measure cmd, do not invoke; "
                         "otherwise alias of --list")
    tm.add_argument("--checkout", default=None,
                    help="checkout / worktree for --calibrate (repeated measure, "
                         "no rebuilds)")
    tm.add_argument("--rounds", type=int, default=None,
                    help="with --calibrate: measure rounds (default 4; must be >= 2)")
    tm.add_argument("--record", action="store_true",
                    help="append verdict to lessons + permtree with fingerprint")
    tm.add_argument("--fn", default=None,
                    help="permtree fn label when --record (default terminal-gate)")
    tm.add_argument("--hypothesis", default=None,
                    help="hypothesis text when --record (default: terminal gate on fn)")
    tm.add_argument("--events-ref", default=None, dest="events_ref",
                    help="events_ref when --record (path to attempt evidence)")
    tm.add_argument("--update-manifest", default=None, dest="update_manifest",
                    help="stamp terminal fields onto manifest.json (path or run dir)")

    tc = sub.add_parser(
        "terminal-calibrate",
        help="[deprecated] alias of `aro terminal --calibrate`")
    _add_terminal_calibrate_args(tc)

    sc = sub.add_parser(
        "selfcheck",
        help="host measurement health: probe A/A spread + tool fingerprint + "
             "optional pin check; writes .aro-runs/selfcheck/<spec>.json marker "
             "required by icount/terminal gates (re-run after tool changes / "
             "every 14 days). --rows checks floor row-set integrity only "
             "(NOT row-level A/A — that is `aro terminal --calibrate`)")
    sc.add_argument("spec")
    sc.add_argument(
        "--rows", action="store_true",
        help="also verify every calibrated floor row appears in a measure "
             "output (row-set integrity + drift warning). Does NOT run "
             "row-level A/A — that is `aro terminal --calibrate`'s job")

    c = sub.add_parser("clean", help="[deprecated] remove a spec's orphaned worktrees + "
                                     "target dirs (explicit, printed; never a "
                                     "background sweep)")
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

    # --- verify-patch / reverify alias / hotpath / ablate / init ---------------
    v = sub.add_parser("verify-patch", help="[deprecated] re-score a recorded patch "
                                            "through the full judge")
    v.add_argument("patch")
    v.add_argument("--spec", required=True)
    v.add_argument("--ab-pairs", type=int, default=4, dest="ab_pairs")
    v.add_argument("--aa-runs", type=int, default=3, dest="aa_runs")
    v.add_argument("--out", default=None)
    v.add_argument("--reuse-out", action="store_true", dest="reuse_out")

    rv = sub.add_parser(
        "reverify",
        help="[deprecated] alias of `aro recheck candidates`")
    _add_recheck_candidates_args(rv)

    ab = sub.add_parser(
        "ablate",
        help="per-entry terminal attribution along the acceptance chain; "
             "propose the largest shippable sub-bundle under row-family policy. "
             "Proposal tool only — never stamps the manifest; certify survivors "
             "with `aro terminal`.")
    ab.add_argument("--spec", required=True,
                    help="target JSON (terminal config + row-family policy)")
    ab.add_argument("--out", required=True,
                    help="campaign run dir: reads manifest.json + aN/patches/, "
                         "writes ablate.json")
    ab.add_argument("--orders", default=None,
                    help="comma-separated 1-based orders to attribute "
                         "(e.g. 1,3,8); others still apply for compounding")
    ab.add_argument("--rounds", type=int, default=None,
                    help="measure rounds per prefix (default: spec / env)")
    ab.add_argument("--upgrade-rounds", type=int, default=None,
                    dest="upgrade_rounds",
                    help="one-shot re-measure rounds for band-zone entries "
                         f"(default {5})")
    ab.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print the attribution plan without measuring")

    h = sub.add_parser("hotpath", help="[deprecated] observe-only: profile the real "
                                       "hot path")
    h.add_argument("spec")

    # --- init ------------------------------------------------------------------
    ini = sub.add_parser(
        "init",
        help="scaffold a minimal target spec + two probe templates from a Rust repo "
             "(flag-driven, non-interactive; agents run it)")
    ini.add_argument("--repo", required=True,
                     help="path to the target Rust repo (Cargo.toml / workspace)")
    ini.add_argument("--name", default=None,
                     help="spec slug (targets/<name>.json); default: package name")
    ini.add_argument("--package", default=None,
                     help="cargo package to optimize (required for multi-member "
                          "workspaces)")
    ini.add_argument("--force", action="store_true",
                     help="overwrite existing targets/<name>.json and probe files")

    return p


def main(argv=None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    raw, bare_recheck_note = _normalize_recheck_argv(raw)
    args = build_parser().parse_args(raw)

    if bare_recheck_note:
        _note(bare_recheck_note)
    if args.cmd in _ALIAS_NOTES:
        _note(_ALIAS_NOTES[args.cmd])
    if args.cmd in _DEPRECATED_CMDS:
        _deprecated_warning(args.cmd)

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
        return _dispatch_recheck(args)
    if args.cmd == "recheck-debts":
        from . import recheck_debts
        return recheck_debts.cli(args)
    if args.cmd == "terminal":
        return _dispatch_terminal(args)
    if args.cmd == "terminal-calibrate":
        from . import terminal
        return terminal.calibrate_cli(args)
    if args.cmd == "selfcheck":
        from . import selfcheck
        return selfcheck.cli(args)
    if args.cmd == "coverage":
        from . import coverage
        return coverage.cli(args)
    if args.cmd == "next":
        from . import next as nextmod
        return nextmod.cli(args)
    if args.cmd == "verify-patch":
        from . import verify
        return verify.cli(args)
    if args.cmd == "reverify":
        from . import reverify
        return reverify.cli(args)
    if args.cmd == "ablate":
        from . import ablate
        return ablate.cli(args)
    if args.cmd == "hotpath":
        return _hotpath(args)
    if args.cmd == "init":
        from . import init as initmod
        return initmod.cli(args)
    raise SystemExit(f"unknown command {args.cmd!r}")


def _dispatch_recheck(args) -> None:
    action = getattr(args, "recheck_action", None) or "staleness"
    if action == "staleness":
        from . import recheck
        return recheck.cli(args)
    if action == "debts":
        from . import recheck_debts
        return recheck_debts.cli(args)
    if action == "candidates":
        from . import reverify
        return reverify.cli(args)
    raise SystemExit(f"unknown recheck action {action!r}")


def _dispatch_terminal(args) -> None:
    from . import terminal
    if getattr(args, "calibrate", False):
        # --calibrate is mutex with --list/--rejudge via argparse; also bar measure.
        if getattr(args, "baseline", None) or getattr(args, "candidate", None):
            raise SystemExit(
                "aro terminal: --calibrate is mutually exclusive with "
                "--baseline/--candidate"
            )
        if not getattr(args, "checkout", None):
            raise SystemExit("aro terminal --calibrate: --checkout DIR is required")
        return terminal.calibrate_cli(args)
    # --dry-run without --calibrate remains list-mode (historical alias).
    return terminal.cli(args)


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
