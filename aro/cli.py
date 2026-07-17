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

# Former top-level commands / aliases: short error naming the live replacement.
# Not a shim — no parse, no dispatch. Typing a key exits 2 with this message.
REMOVED_COMMANDS = {
    "run": "aro sweep <spec> --attempt",
    "plan": "aro init --repo <path>",
    "union": "aro tree / memory/permtree ledgers",
    "next": "aro pipeline",
    "coverage": "aro sweep --workloads (dark-region artifacts if present)",
    "clean": "manual worktree/run-dir cleanup",
    "verify-patch": "aro recheck candidates",
    "hotpath": "aro sweep (frontier map profiles the hot path)",
    "recheck-debts": "aro recheck debts",
    "reverify": "aro recheck candidates",
    "terminal-calibrate": "aro terminal --calibrate",
}


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
    p.add_argument(
        "--baseline", default=None, metavar="REF",
        help="override the spec's baseline_ref for this replay only "
             "(resolved in the target repo; reverify.json records the "
             "effective baseline_sha). Does not mutate the spec file.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aro",
        description="ARO: autonomous optimization loop; the deterministic judge is the moat.")
    sub = p.add_subparsers(dest="cmd", required=True)

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
    s.add_argument(
        "--allow-stale-baseline", action="store_true",
        dest="allow_stale_baseline",
        help="override the baseline preflight that aborts --attempt when "
             "recheck.assess reports region churn / re-pin (loud warn event; "
             "default is fail-closed)")
    s.add_argument(
        "--seeds", default=None, metavar="FILE",
        help="optional seeds.json / reattempt-queue: bias frontier attempt "
             "order so listed fns run FIRST (ordering only; non-frontier "
             "seeds emit seed_skipped events; no gate/judge changes)")

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

    # --- recheck namespace (staleness / debts / candidates) -----------------------
    rc = sub.add_parser(
        "recheck",
        help="recheck family: staleness (baseline churn), debts (Ir re-adjudication), "
             "candidates (replay correctness gates on a frozen manifest)")
    rc_sub = rc.add_subparsers(dest="recheck_action", required=True)

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
                               "optional --update-manifest stamps via apply_terminal; "
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
    tm.add_argument(
        "--max-est-secs", type=float, default=None, dest="max_est_secs",
        help="probe-lane cost preflight: abort calibrate/measure when "
             "extrapolated wall time exceeds this many seconds "
             "(default 14400 = 4h). Dry-run always prints the estimate "
             "without aborting")
    tm.add_argument(
        "--accept-cost", action="store_true", dest="accept_cost",
        help="probe-lane: proceed even when the cost preflight estimate "
             "exceeds --max-est-secs (loud note on stderr)")
    tm.add_argument("--record", action="store_true",
                    help="append verdict to lessons + permtree with fingerprint")
    tm.add_argument("--fn", default=None,
                    help="permtree fn label when --record (default terminal-gate)")
    tm.add_argument("--hypothesis", default=None,
                    help="hypothesis text when --record (default: terminal gate on fn)")
    tm.add_argument("--events-ref", default=None, dest="events_ref",
                    help="events_ref when --record (path to attempt evidence)")
    tm.add_argument("--update-manifest", default=None, dest="update_manifest",
                    help="stamp terminal fields onto manifest.json (path or run dir); "
                         "works with measure and with --rejudge (same apply_terminal path)")
    tm.add_argument(
        "--orders", default=None,
        help="1-based accepted orders covered by this measurement "
             "(comma/range, e.g. 1,3,5-8 or 1-13). Written into terminal.json "
             "as measured_orders; apply_terminal stamps only those entries "
             "(others get TERMINAL_NOT_MEASURED, no stamp). With --rejudge, "
             "explicit --orders wins over the doc's measured_orders")

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

    # --- ablate / init --------------------------------------------------------
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

    # --- certify (recheck → terminal → prune? → stamp) -----------------------
    cf = sub.add_parser(
        "certify",
        help="one command from candidates to stamped manifest: recheck "
             "candidates → terminal measure → decision-table dispatch "
             "(MIXED: greedy attribution prune ≤2 rounds) → stamp via "
             "apply_terminal. Exit 0 stamped / 2 work-order stop / 1 error.")
    cf.add_argument("spec",
                    help="target JSON (terminal + correctness gates)")
    cf.add_argument(
        "--manifest", required=True, metavar="DIR",
        help="campaign run dir (manifest.json + patches; writes "
             "reverify.json, terminal-cN.json, certify-prune.jsonl, stamps)")
    cf.add_argument(
        "--orders", default=None,
        help="1-based accepted orders to gate/measure "
             "(comma/range, e.g. 1,3,5-8); default: all reverify-pass survivors")
    cf.add_argument(
        "--from", dest="from_stage", default="recheck",
        choices=("recheck", "terminal", "prune", "stamp"),
        help="surgical re-entry stage (default recheck); earlier artifacts "
             "in the run dir are reused when present")

    # --- ship (gate / package / conformance / open / watch) -------------------
    sh = sub.add_parser(
        "ship",
        help="ship family: gate (baseline currency) + package (certified "
             "branch + PR body) + conformance (quality proof) + open "
             "(push + gh pr create under machine gates) + watch "
             "(PR outcome → campaign ledger / re-attempt seeds)")
    sh_sub = sh.add_subparsers(dest="ship_action", required=True)
    shg = sh_sub.add_parser(
        "gate",
        help="clearance check: every mergeable terminal_stamp.baseline_sha "
             "must equal the ship target head (fail-closed on legacy stamps)")
    shg.add_argument("spec",
                     help="target JSON (repo path + optional ship_target)")
    shg.add_argument(
        "--manifest", required=True, metavar="PATH",
        help="campaign run dir or manifest.json path")
    shg.add_argument(
        "--target", default=None, metavar="REMOTE/BRANCH",
        help="ship target ref (default: spec ship_target or origin/main)")
    shg.add_argument(
        "--no-fetch", action="store_true", dest="no_fetch",
        help="resolve the target ref locally without git fetch "
             "(default fetches remote/branch first; fetch failure is a gate error)")
    shp = sh_sub.add_parser(
        "package",
        help="inline gate + worktree at certified head + apply mergeable "
             "patches + single certified-set commit + write pr_body.md")
    shp.add_argument("spec",
                     help="target JSON (repo path + optional ship_target)")
    shp.add_argument(
        "--manifest", required=True, metavar="PATH",
        help="campaign run dir or manifest.json path "
             "(pr_body.md is written next to the manifest)")
    shp.add_argument(
        "--target", default=None, metavar="REMOTE/BRANCH",
        help="ship target ref (default: spec ship_target or origin/main)")
    shp.add_argument(
        "--no-fetch", action="store_true", dest="no_fetch",
        help="resolve the target ref locally without git fetch")
    shp.add_argument(
        "--branch", default=None, metavar="NAME",
        help="PR branch name (default: aro/ship-<runname>)")
    shp.add_argument(
        "--workdir", default=None, metavar="DIR",
        help="worktree path (default: <repo.parent>/.aro-worktrees/ship-<runname>)")
    shc = sh_sub.add_parser(
        "conformance",
        help="run target-repo quality checks on the PR-branch checkout; "
             "write a machine record bound to head_sha (fail-closed)")
    shc.add_argument("spec",
                     help="target JSON (must define ship_conformance)")
    shc.add_argument(
        "--workdir", required=True, metavar="DIR",
        help="PR-branch checkout (must be a clean git workdir; "
             "uncommitted tracked changes are rejected)")
    shc.add_argument(
        "--out", default=None, metavar="PATH",
        help="conformance record path "
             "(default: <workdir>/.aro-conformance.json)")
    sho = sh_sub.add_parser(
        "open",
        help="machine-gated git push + gh pr create (re-gate, green "
             "conformance record bound to HEAD, clean tree, post-cert "
             "commit whitelist; fail-closed)")
    sho.add_argument("spec",
                     help="target JSON (optional ship_remote / pr_labels)")
    sho.add_argument(
        "--manifest", required=True, metavar="PATH",
        help="campaign run dir or manifest.json path "
             "(reads <out_dir>/pr_body.md)")
    sho.add_argument(
        "--workdir", required=True, metavar="DIR",
        help="packaged PR-branch checkout")
    sho.add_argument(
        "--record", default=None, metavar="PATH",
        help="conformance record path "
             "(default: <workdir>/.aro-conformance.json)")
    sho.add_argument(
        "--title", default=None, metavar="TITLE",
        help="PR title (default: certified-set commit subject)")
    sho.add_argument(
        "--target", default=None, metavar="REMOTE/BRANCH",
        help="ship target ref (default: spec ship_target or origin/main)")
    sho.add_argument(
        "--no-fetch", action="store_true", dest="no_fetch",
        help="resolve the target ref locally without git fetch")
    shw = sh_sub.add_parser(
        "watch",
        help="one-shot poll of an opened PR: stamp shipped on merge, or "
             "harvest review feedback + reattempt queue on close / "
             "changes-requested (not a daemon). --all settles every open "
             "entry in the ship ledger without hand-fed --pr")
    shw.add_argument("spec",
                     help="target JSON (CLI symmetry; poll keys off --manifest + --pr)")
    shw.add_argument(
        "--manifest", default=None, metavar="PATH",
        help="campaign run dir or manifest.json path "
             "(required unless --all)")
    shw.add_argument(
        "--pr", default=None, metavar="URL|NUMBER",
        help="PR URL or number (required unless --all)")
    shw.add_argument(
        "--all", dest="watch_all", action="store_true",
        help="settle every status=open entry in the ship ledger "
             "(requires --runs-root; ignores --pr/--manifest)")
    shw.add_argument(
        "--runs-root", default=None, metavar="DIR",
        help="runs root containing <spec>-ships.jsonl and run dirs "
             "(required with --all; default convention: .aro-runs)")

    # --- pipeline (sweep → certify → gate → package | resume → open) ----------
    pipe = sub.add_parser(
        "pipeline",
        help="one checkpointed command from campaign to opened PR. "
             "Without --manifest: stage-0 bootstrap (settle ledger, re-pin "
             "baseline, seed, auto-name out-dir) then the stage chain. "
             "With --manifest: T44 path only (no bootstrap). Re-run / "
             "--continue resumes from the first incomplete stage.")
    pipe.add_argument(
        "spec",
        help="target JSON (repo path + terminal + ship_* slots)")
    pipe.add_argument(
        "--manifest", default=None, metavar="DIR",
        help="campaign run dir (T44 path: no bootstrap). Omit for stage-0 "
             "bootstrap which auto-names <runs-root>/<spec>-auto-<YYYYMMDD>")
    pipe.add_argument(
        "--continue", dest="pipeline_continue", action="store_true",
        help="resume from the first incomplete stage (same as a plain "
             "re-run when pipeline-state.json exists; kept for UX clarity)")
    pipe.add_argument(
        "--fresh", action="store_true",
        help="delete pipeline-state.json and start over (never deletes "
             "campaign artifacts)")
    pipe.add_argument(
        "--no-sweep", dest="no_sweep", action="store_true",
        help="mark sweep skipped and continue from an existing campaign "
             "out-dir (do not re-run --attempt)")
    pipe.add_argument(
        "--workdir", default=None, metavar="DIR",
        help="ship package worktree path (default: "
             "<repo.parent>/.aro-worktrees/ship-<runname>)")
    pipe.add_argument(
        "--branch", default=None, metavar="NAME",
        help="PR branch name for ship package "
             "(default: aro/ship-<runname>)")
    pipe.add_argument(
        "--runs-root", default=None, metavar="DIR",
        help="root for ship ledger + auto-named out-dirs "
             "(default: .aro-runs); used by stage-0 bootstrap")
    pipe.add_argument(
        "--skip-ledger", dest="skip_ledger", action="store_true",
        help="bootstrap: skip settle-ledger loudly (fail-open override; "
             "default is fail-closed on gh/network failure)")

    return p


def main(argv=None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and not raw[0].startswith("-") and raw[0] in REMOVED_COMMANDS:
        repl = REMOVED_COMMANDS[raw[0]]
        print(f"error: 'aro {raw[0]}' removed; use `{repl}`", file=sys.stderr)
        raise SystemExit(2)
    args = build_parser().parse_args(raw)

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
    if args.cmd == "recheck":
        return _dispatch_recheck(args)
    if args.cmd == "terminal":
        return _dispatch_terminal(args)
    if args.cmd == "selfcheck":
        from . import selfcheck
        return selfcheck.cli(args)
    if args.cmd == "ablate":
        from . import ablate
        return ablate.cli(args)
    if args.cmd == "init":
        from . import init as initmod
        return initmod.cli(args)
    if args.cmd == "certify":
        from . import certify
        return certify.cli(args)
    if args.cmd == "pipeline":
        from . import pipeline
        return pipeline.cli(args)
    if args.cmd == "ship":
        from . import ship
        return ship.cli(args)
    raise SystemExit(f"unknown command {args.cmd!r}")


def _dispatch_recheck(args) -> None:
    action = getattr(args, "recheck_action", None)
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
