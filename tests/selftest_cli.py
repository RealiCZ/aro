"""T28: CLI consolidation — recheck namespace, terminal --calibrate, deprecation shims."""
from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


def case_44():
    """T28: CLI routing, aliases, deprecation shims, mutual exclusion."""
    print("=== case 44: CLI consolidation (recheck / terminal --calibrate / shims) ===")
    from aro import cli as _cli
    from aro.cli import build_parser, main

    p = build_parser()

    # --- routing: recheck candidates → same args namespace as reverify ----------
    a = p.parse_args([
        "recheck", "candidates", "--spec", "t.json", "--out", "/tmp/x",
        "--orders", "1,3", "--apply",
    ])
    assert a.cmd == "recheck" and a.recheck_action == "candidates"
    assert a.spec == "t.json" and a.out == "/tmp/x"
    assert a.orders == "1,3" and a.apply is True
    a_old = p.parse_args([
        "reverify", "--spec", "t.json", "--out", "/tmp/x",
        "--orders", "1,3", "--apply",
    ])
    for attr in ("spec", "out", "orders", "apply"):
        assert getattr(a, attr) == getattr(a_old, attr), attr
    print("#44a OK: recheck candidates parses like reverify")

    ad = p.parse_args(["recheck", "debts", "targets/x.json", "--list-only", "--dry-run"])
    assert ad.cmd == "recheck" and ad.recheck_action == "debts"
    assert ad.spec == "targets/x.json" and ad.list_only is True and ad.dry_run is True
    as_ = p.parse_args(["recheck", "staleness", "targets/x.json", "--ref", "main", "--json"])
    assert as_.cmd == "recheck" and as_.recheck_action == "staleness"
    assert as_.spec == "targets/x.json" and as_.ref == "main" and as_.json is True
    print("#44b OK: recheck debts / staleness parse")

    at = p.parse_args([
        "terminal", "targets/x.json", "--calibrate",
        "--checkout", "/wt", "--rounds", "3", "--dry-run",
    ])
    assert at.cmd == "terminal" and at.calibrate is True
    assert at.checkout == "/wt" and at.rounds == 3 and at.dry_run is True
    atc = p.parse_args([
        "terminal-calibrate", "targets/x.json",
        "--checkout", "/wt", "--rounds", "3", "--dry-run",
    ])
    assert atc.cmd == "terminal-calibrate"
    assert atc.checkout == "/wt" and atc.rounds == 3 and atc.dry_run is True
    print("#44c OK: terminal --calibrate / terminal-calibrate parse")

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
    print("#44d OK: terminal --calibrate mutex with --rejudge/--list")

    # [deprecated] help prefix on shims + aliases (via _choices_actions)
    for action in p._subparsers._actions:
        choices_actions = getattr(action, "_choices_actions", None)
        if not choices_actions:
            continue
        by_dest = {ca.dest: ca for ca in choices_actions}
        for name in ("run", "plan", "union", "next", "coverage", "clean",
                     "verify-patch", "hotpath", "reverify", "recheck-debts",
                     "terminal-calibrate"):
            ca = by_dest.get(name)
            assert ca is not None, name
            assert ca.help and ca.help.startswith("[deprecated]"), (name, ca.help)
    print("#44e OK: [deprecated] help prefix on shims + aliases")

    # recheck namespace documents subactions
    for action in p._subparsers._actions:
        ch = getattr(action, "choices", None) or {}
        if "recheck" in ch:
            rc_h = ch["recheck"].format_help()
            assert "staleness" in rc_h and "debts" in rc_h and "candidates" in rc_h
            break
    else:
        raise AssertionError("recheck subparser missing")
    print("#44f OK: recheck namespace documents subactions")

    # --- dispatch spies --------------------------------------------------------
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
    assert "deprecated" not in e and "note:" not in e
    print("#44g OK: recheck candidates → reverify.cli")

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
    print("#44h OK: recheck debts/staleness dispatch")

    _o, e, seen = _run_main(
        ["terminal", "targets/x.json", "--calibrate", "--checkout", "/wt", "--dry-run"],
        spy_target=_tm, attr="calibrate_cli",
    )
    assert seen and seen[0].checkout == "/wt" and seen[0].calibrate is True
    assert _o == ""
    print("#44i OK: terminal --calibrate → calibrate_cli")

    # aliases: one notice line on stderr, stdout clean
    _o, e, seen = _run_main(
        ["reverify", "--spec", "t.json", "--out", "/tmp/x"],
        spy_target=_rv,
    )
    assert seen and seen[0].spec == "t.json"
    assert _o == ""
    assert e.strip() == "note: 'aro reverify' is now 'aro recheck candidates' (alias kept)"
    print("#44j OK: reverify alias notice")

    _o, e, seen = _run_main(
        ["recheck-debts", "targets/x.json", "--list-only"],
        spy_target=_rd,
    )
    assert seen and _o == ""
    assert e.strip() == "note: 'aro recheck-debts' is now 'aro recheck debts' (alias kept)"
    print("#44k OK: recheck-debts alias notice")

    _o, e, seen = _run_main(
        ["terminal-calibrate", "targets/x.json", "--checkout", "/wt", "--dry-run"],
        spy_target=_tm, attr="calibrate_cli",
    )
    assert seen and _o == ""
    assert e.strip() == (
        "note: 'aro terminal-calibrate' is now 'aro terminal --calibrate' (alias kept)"
    )
    print("#44l OK: terminal-calibrate alias notice")

    # bare recheck → staleness + notice
    _o, e, seen = _run_main(
        ["recheck", "targets/x.json", "--ref", "HEAD"],
        spy_target=_rc,
    )
    assert seen and seen[0].spec == "targets/x.json" and seen[0].ref == "HEAD"
    assert _o == ""
    assert e.strip() == "note: 'aro recheck' is now 'aro recheck staleness' (alias kept)"
    print("#44m OK: bare recheck → staleness + notice")

    # deprecation shims
    import aro.plan as _plan
    import aro.union as _union
    import aro.next as _next
    import aro.coverage as _cov
    import aro.clean as _clean
    import aro.verify as _ver
    import aro.__main__ as _am

    shims = [
        (["run", "targets/x.json"], _am, "run_cli", "run"),
        (["plan", "goal", "/repo"], _plan, "cli", "plan"),
        (["union"], _union, "cli", "union"),
        (["next", "targets/x.json"], _next, "cli", "next"),
        (["coverage", "targets/x.json"], _cov, "cli", "coverage"),
        (["clean", "targets/x.json"], _clean, "cli", "clean"),
        (["verify-patch", "p.txt", "--spec", "t.json"], _ver, "cli", "verify-patch"),
    ]
    for argv, mod, attr, name in shims:
        _o, e, seen = _run_main(argv, spy_target=mod, attr=attr)
        assert seen, name
        assert _o == "", name
        assert e.strip() == (
            f"warning: 'aro {name}' is deprecated "
            f"(unused in production; may be removed in a future release)"
        ), (name, e)

    seen_hp = []

    def _hp(args):
        seen_hp.append(args)

    out, err = io.StringIO(), io.StringIO()
    with patch.object(_cli, "_hotpath", _hp):
        with redirect_stdout(out), redirect_stderr(err):
            main(["hotpath", "targets/x.json"])
    assert seen_hp and out.getvalue() == ""
    assert err.getvalue().strip() == (
        "warning: 'aro hotpath' is deprecated "
        "(unused in production; may be removed in a future release)"
    )
    print("#44n OK: 8 deprecation shims warn + dispatch")
    print("#44 OK: CLI consolidation")
