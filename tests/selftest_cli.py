"""T28 / T46: CLI surface — live commands, REMOVED_COMMANDS map, no shims."""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


def case_47():
    """T46: removed names → exit 2 + replacement; live commands still parse/dispatch."""
    print("=== case 47: CLI surface (REMOVED_COMMANDS + live routing) ===")
    from aro.cli import REMOVED_COMMANDS, build_parser, main

    p = build_parser()

    # --- live routing: recheck candidates / debts / staleness -----------------
    a = p.parse_args([
        "recheck", "candidates", "--spec", "t.json", "--out", "/tmp/x",
        "--orders", "1,3", "--apply",
    ])
    assert a.cmd == "recheck" and a.recheck_action == "candidates"
    assert a.spec == "t.json" and a.out == "/tmp/x"
    assert a.orders == "1,3" and a.apply is True
    print("#47a OK: recheck candidates parses")

    ad = p.parse_args(["recheck", "debts", "targets/x.json", "--list-only", "--dry-run"])
    assert ad.cmd == "recheck" and ad.recheck_action == "debts"
    assert ad.spec == "targets/x.json" and ad.list_only is True and ad.dry_run is True
    as_ = p.parse_args(["recheck", "staleness", "targets/x.json", "--ref", "main", "--json"])
    assert as_.cmd == "recheck" and as_.recheck_action == "staleness"
    assert as_.spec == "targets/x.json" and as_.ref == "main" and as_.json is True
    print("#47b OK: recheck debts / staleness parse")

    at = p.parse_args([
        "terminal", "targets/x.json", "--calibrate",
        "--checkout", "/wt", "--rounds", "3", "--dry-run",
    ])
    assert at.cmd == "terminal" and at.calibrate is True
    assert at.checkout == "/wt" and at.rounds == 3 and at.dry_run is True
    print("#47c OK: terminal --calibrate parses")

    # mutual exclusion → argparse SystemExit(2)
    try:
        p.parse_args([
            "terminal", "targets/x.json", "--calibrate", "--rejudge", "x.json",
            "--checkout", "/wt",
        ])
        raise AssertionError("expected argparse error for --calibrate --rejudge")
    except SystemExit as se:
        assert se.code == 2
    try:
        p.parse_args([
            "terminal", "targets/x.json", "--calibrate", "--list",
            "--checkout", "/wt",
        ])
        raise AssertionError("expected argparse error for --calibrate --list")
    except SystemExit as se:
        assert se.code == 2
    print("#47d OK: terminal --calibrate mutex with --rejudge/--list")

    # removed names must NOT appear as subparsers
    for action in p._subparsers._actions:
        choices = getattr(action, "choices", None) or {}
        if not choices:
            continue
        for name in REMOVED_COMMANDS:
            assert name not in choices, name
    print("#47e OK: removed names absent from argparse choices")

    # recheck namespace documents subactions; bare recheck requires an action
    for action in p._subparsers._actions:
        ch = getattr(action, "choices", None) or {}
        if "recheck" in ch:
            rc_h = ch["recheck"].format_help()
            assert "staleness" in rc_h and "debts" in rc_h and "candidates" in rc_h
            break
    else:
        raise AssertionError("recheck subparser missing")
    try:
        p.parse_args(["recheck", "targets/x.json"])
        raise AssertionError("bare recheck without action must fail")
    except SystemExit as se:
        assert se.code == 2
    print("#47f OK: recheck subactions required; bare form rejected")

    # --- live commands still parse --------------------------------------------
    live = [
        ["sweep", "targets/x.json"],
        ["tree", "/tmp/out"],
        ["manifest", "/tmp/out"],
        ["serve", "/tmp/out"],
        ["terminal", "targets/x.json", "--list"],
        ["selfcheck", "targets/x.json"],
        ["ablate", "--spec", "t.json", "--out", "/tmp/x"],
        ["init", "--repo", "/tmp/r"],
        ["certify", "targets/x.json", "--manifest", "/tmp/m"],
        ["pipeline", "targets/x.json", "--manifest", "/tmp/m"],
        ["ship", "gate", "targets/x.json", "--manifest", "/tmp/m"],
        ["recheck", "staleness", "targets/x.json"],
        ["recheck", "debts", "targets/x.json"],
        ["recheck", "candidates", "--spec", "t.json", "--out", "/tmp/x"],
    ]
    for argv in live:
        args = p.parse_args(argv)
        assert args.cmd == argv[0], argv
    print("#47g OK: live commands still parse")

    # --- REMOVED_COMMANDS: every key → exit 2 + names replacement -------------
    assert set(REMOVED_COMMANDS) == {
        "run", "plan", "union", "next", "coverage", "clean", "verify-patch",
        "hotpath", "recheck-debts", "reverify", "terminal-calibrate",
    }
    for name, repl in sorted(REMOVED_COMMANDS.items()):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            try:
                main([name])
                raise AssertionError(f"{name}: expected SystemExit(2)")
            except SystemExit as se:
                assert se.code == 2, (name, se.code)
        msg = err.getvalue()
        assert "removed" in msg, (name, msg)
        assert repl in msg or repl.split()[0] in msg, (name, repl, msg)
        assert f"aro {name}" in msg or f"'{name}'" in msg, (name, msg)
    print("#47h OK: every REMOVED name → exit 2 + replacement")

    # --- dispatch spies: live paths only --------------------------------------
    def _run_main(argv, *, spy_target, attr="cli"):
        seen = []

        def _spy(args):
            seen.append(args)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(spy_target, attr, _spy):
            with redirect_stdout(out), redirect_stderr(err):
                main(argv)
        return out.getvalue(), err.getvalue(), seen

    import aro.reverify as _rv
    import aro.recheck as _rc
    import aro.recheck_debts as _rd
    import aro.terminal as _tm

    _o, e, seen = _run_main(
        ["recheck", "candidates", "--spec", "t.json", "--out", "/tmp/x"],
        spy_target=_rv,
    )
    assert seen and seen[0].spec == "t.json" and seen[0].out == "/tmp/x"
    assert _o == ""
    assert "removed" not in e and "deprecated" not in e and "note:" not in e
    print("#47i OK: recheck candidates → reverify.cli")

    _o, e, seen = _run_main(
        ["recheck", "debts", "targets/x.json", "--list-only"],
        spy_target=_rd,
    )
    assert seen and seen[0].list_only is True
    assert _o == ""
    _o, e, seen = _run_main(
        ["recheck", "staleness", "targets/x.json", "--json"],
        spy_target=_rc,
    )
    assert seen and seen[0].json is True and seen[0].spec == "targets/x.json"
    assert _o == ""
    print("#47j OK: recheck debts/staleness dispatch")

    _o, e, seen = _run_main(
        ["terminal", "targets/x.json", "--calibrate", "--checkout", "/wt", "--dry-run"],
        spy_target=_tm, attr="calibrate_cli",
    )
    assert seen and seen[0].checkout == "/wt" and seen[0].calibrate is True
    assert _o == ""
    print("#47k OK: terminal --calibrate → calibrate_cli")
    print("#47 OK: CLI surface after command-prune")
