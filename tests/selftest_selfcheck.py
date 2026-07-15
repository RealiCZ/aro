from __future__ import annotations

import json
import tempfile
from pathlib import Path

def case_33():
    """T9: selfcheck gate + env_fingerprint (hermetic)."""
    import importlib
    import io
    import os
    import subprocess as _sp
    from contextlib import redirect_stderr
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from aro import selfcheck as _sc
    from aro import terminal as _tm
    from aro import lessons as _les
    from aro import permtree as _pt
    from aro import cli as _cli
    from aro.icount import ICountResult

    # --- version probe parsing + cargo-codspeed exit-1 quirk -----------------
    def _vrunner(cmd):
        c0 = cmd[0] if cmd else ""
        if c0 == "codspeed":
            return "codspeed 4.18.3\n"
        if c0 == "cargo" and "codspeed" in cmd:
            # clap quirk: nonzero exit while printing banner — runner returns text only
            return "cargo-codspeed 5.0.1\nerror: unrecognized subcommand\n"
        if c0 == "valgrind":
            return "valgrind-3.26.0.codspeed5\n"
        if c0 == "rustc":
            return "rustc 1.80.0 (aaa 2024-01-01)\n"
        return ""

    vers = _sc.probe_tool_versions(runner=_vrunner, use_cache=False)
    assert vers["codspeed"] == "4.18.3", vers
    assert vers["cargo-codspeed"] == "5.0.1", vers
    assert "3.26.0" in vers["valgrind"], vers
    assert vers["rustc"].startswith("1.80.0"), vers
    fp = _sc.env_fingerprint(vers)
    assert fp == ("codspeed=4.18.3;cargo-codspeed=5.0.1;"
                  "valgrind=3.26.0.codspeed5;rustc=1.80.0"), fp
    # missing tools → unknown
    empty = _sc.env_fingerprint(
        {"codspeed": "", "cargo-codspeed": "", "valgrind": "", "rustc": ""})
    assert empty == ("codspeed=unknown;cargo-codspeed=unknown;"
                     "valgrind=unknown;rustc=unknown")
    # no version-like token (cargo 'error: no such command') → 'unknown', never raw
    assert _sc._first_version_token(
        "error: no such command: `codspeed`\n") == "unknown"
    assert _sc._first_version_token(
        "error: no such command\n") == "unknown"
    assert "error" not in _sc._first_version_token(
        "error: no such command: `codspeed`\n")
    # tool banners naming themselves 'codspeed-runner' (the real 4.18.3 CLI
    # shape) must yield the version, not the 'runner' name fragment
    assert _sc._first_version_token("codspeed-runner 4.18.3\n") == "4.18.3"
    vers_runner = _sc.probe_tool_versions(
        runner=lambda cmd: ("codspeed-runner 4.18.3\n"
                            if cmd and cmd[0] == "codspeed" else _vrunner(cmd)),
        use_cache=False)
    assert vers_runner["codspeed"] == "4.18.3", vers_runner
    print("#44a OK: version probe parse + cargo-codspeed exit-1 + fingerprint")

    # --- pin check (exact / token-boundary; not bidirectional substring) -----
    assert _sc.check_pinned_tools(vers, {}) is None
    assert _sc.check_pinned_tools(vers, {
        "codspeed": "4.18.3", "valgrind": "3.26.0.codspeed5"}) is None
    # build-tag suffix still matches via token-boundary prefix
    assert _sc.check_pinned_tools(
        {"valgrind": "3.26.0.codspeed5"}, {"valgrind": "3.26.0"}) is None
    pin_err = _sc.check_pinned_tools(vers, {"codspeed": "9.9.9"})
    assert pin_err and "expected" in pin_err and "9.9.9" in pin_err
    # pin '3.26.0' must NOT match found '13.26.0' (old bidirectional substring bug)
    pin_mid = _sc.check_pinned_tools(
        {"valgrind": "13.26.0"}, {"valgrind": "3.26.0"})
    assert pin_mid and "3.26.0" in pin_mid and "13.26.0" in pin_mid, pin_mid
    print("#44b OK: pin check match / mismatch / no mid-token false positive")

    # --- process cache: only SUCCESSFUL probes are cached --------------------
    # Exercise the real cache path (runner=None, no override) by patching
    # `_run_version_cmd`: first probe fails transiently → not cached; second
    # succeeds and is cached; third hits the cache.
    _sc.clear_version_cache()
    _sc.set_version_runner(None)
    state = {"phase": "fail"}
    orig_run_version = _sc._run_version_cmd

    def _phase_cmd(cmd, timeout=15.0):
        if state["phase"] == "fail":
            return ""
        return _vrunner(list(cmd))

    _sc._run_version_cmd = _phase_cmd
    try:
        v1 = _sc.probe_tool_versions(use_cache=True)
        assert not _sc._probe_succeeded(v1), v1
        assert _sc._VERSION_CACHE is None, "failure must not be cached"
        state["phase"] = "ok"
        v2 = _sc.probe_tool_versions(use_cache=True)
        assert _sc._probe_succeeded(v2), v2
        assert _sc._VERSION_CACHE is not None, "success must be cached"
        # Third call must hit cache (phase flip would break it if re-probed)
        state["phase"] = "fail"
        v3 = _sc.probe_tool_versions(use_cache=True)
        assert v3["codspeed"] == v2["codspeed"] == "4.18.3"
    finally:
        _sc._run_version_cmd = orig_run_version
        _sc.clear_version_cache()
    print("#44b2 OK: version cache stores only successful probes")

    # --- skip_selfcheck_requested: only 1/true/yes (case-insensitive) --------
    for truthy in ("1", "true", "TRUE", "Yes", "yes"):
        os.environ["ARO_SKIP_SELFCHECK"] = truthy
        assert _sc.skip_selfcheck_requested() is True, truthy
    for falsy in ("", "0", "false", "no", "off", "maybe", "2", "TRUEISH"):
        if falsy == "":
            os.environ.pop("ARO_SKIP_SELFCHECK", None)
        else:
            os.environ["ARO_SKIP_SELFCHECK"] = falsy
        assert _sc.skip_selfcheck_requested() is False, repr(falsy)
    os.environ.pop("ARO_SKIP_SELFCHECK", None)
    print("#44b3 OK: skip_selfcheck_requested only 1/true/yes")

    # --- same_binary_spread_pct: two zeros → measurement error, not 0% pass --
    try:
        _sc.same_binary_spread_pct(0, 0)
        assert False, "two zero Ir readings must raise (measurement error)"
    except ValueError as e:
        assert "zero" in str(e).lower() or "measurement" in str(e).lower()
    assert abs(_sc.same_binary_spread_pct(10000, 10040) - 0.3996) < 1e-3
    print("#44b4 OK: same_binary_spread_pct rejects dual-zero Ir")

    # --- marker lifecycle: pass writes; fail no marker -----------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_RUNS_ROOT"] = str(td)
        _sc.clear_version_cache()
        try:
            mpath = td / "selfcheck" / "demo.json"
            # fail: spread too high → no marker
            irs = iter([
                ICountResult(ir=10000, events={"Ir": 10000}),
                ICountResult(ir=11000, events={"Ir": 11000}),  # ~9.5%
            ])

            def _noisy(work, scale=1, cache_sim=False):
                return next(irs)

            sp = SimpleNamespace(
                name="demo", selfcheck_probe_max_pct=0.05, raw={}, pinned_tools=None)
            r = _sc.run_selfcheck(
                sp, icount_fn=_noisy, make_worktree=False,
                version_runner=_vrunner, marker_path_override=mpath)
            assert r.ok is False
            assert r.probe_spread_pct is not None and r.probe_spread_pct > 0.05
            assert not mpath.exists(), "fail must not write marker"

            # fail: pin mismatch → no marker
            sp_pin = SimpleNamespace(
                name="demo", selfcheck_probe_max_pct=1.0, raw={},
                pinned_tools={"codspeed": "0.0.1"})
            irs2 = iter([
                ICountResult(ir=10000, events={"Ir": 10000}),
                ICountResult(ir=10000, events={"Ir": 10000}),
            ])
            r = _sc.run_selfcheck(
                sp_pin, icount_fn=lambda *a, **k: next(irs2),
                make_worktree=False, version_runner=_vrunner,
                marker_path_override=mpath)
            assert r.ok is False
            assert any("pin" in n.lower() for n in r.notes)
            assert not mpath.exists()

            # pass: tight A/A → marker written
            irs3 = iter([
                ICountResult(ir=1000000, events={"Ir": 1000000}),
                ICountResult(ir=1000040, events={"Ir": 1000040}),  # 0.004%
            ])
            r = _sc.run_selfcheck(
                sp, icount_fn=lambda *a, **k: next(irs3),
                make_worktree=False, version_runner=_vrunner,
                marker_path_override=mpath)
            assert r.ok is True, r.notes
            assert mpath.is_file()
            marker = json.loads(mpath.read_text())
            assert "passed_at" in marker
            assert marker["env_fingerprint"] == fp
            assert marker["rounds"] == 2
            assert abs(marker["probe_spread_pct"] - r.probe_spread_pct) < 1e-9
        finally:
            del os.environ["ARO_RUNS_ROOT"]
            _sc.clear_version_cache()
    print("#44c OK: marker lifecycle pass/fail (spread + pin)")

    # --- gate integration: missing / stale / mismatch / skip / valid ---------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_RUNS_ROOT"] = str(td)
        _sc.clear_version_cache()
        try:
            sp = SimpleNamespace(name="gate-demo", raw={})
            # missing marker → hard error
            try:
                _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
                assert False, "missing marker must raise"
            except _sc.SelfcheckError as e:
                assert "no marker" in str(e).lower() or "selfcheck" in str(e).lower()
                assert "selfcheck" in str(e)

            # write a valid marker then pass
            mpath = _sc.marker_path("gate-demo")
            _sc.write_marker("gate-demo", env_fp=fp, probe_spread_pct=0.004)
            got = _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
            assert got == fp

            # stale (>14d) → hard error
            stale_at = (datetime.now(timezone.utc) - timedelta(days=20)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            mpath.write_text(json.dumps({
                "passed_at": stale_at, "env_fingerprint": fp,
                "probe_spread_pct": 0.004, "rounds": 2,
            }) + "\n")
            try:
                _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
                assert False, "stale marker must raise"
            except _sc.SelfcheckError as e:
                assert "days" in str(e) or "old" in str(e)

            # fingerprint mismatch → hard error
            _sc.write_marker(
                "gate-demo",
                env_fp="codspeed=0;cargo-codspeed=0;valgrind=0;rustc=0",
                probe_spread_pct=0.001)
            try:
                _sc.require_selfcheck(sp, runner=_vrunner, use_cache=False)
                assert False, "fp mismatch must raise"
            except _sc.SelfcheckError as e:
                assert "mismatch" in str(e).lower() or "fingerprint" in str(e)

            # ARO_SKIP_SELFCHECK=1 → proceeds with warning, NO version probe
            # (returns None; skip-when-absent — never a fresh fingerprint).
            _sc.write_marker(  # leave a mismatched marker; skip must ignore it
                "gate-demo",
                env_fp="codspeed=0;cargo-codspeed=0;valgrind=0;rustc=0",
                probe_spread_pct=0.001)
            os.environ["ARO_SKIP_SELFCHECK"] = "1"
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    got = _sc.require_selfcheck(
                        sp, runner=_vrunner, use_cache=False)
                assert got is None, "skip must not return a fingerprint"
                assert "ARO_SKIP_SELFCHECK" in err.getvalue()
                assert "WARNING" in err.getvalue()
            finally:
                del os.environ["ARO_SKIP_SELFCHECK"]

            # Hermeticity: under skip, subprocess.run must never fire.
            os.environ["ARO_SKIP_SELFCHECK"] = "1"
            real_run = _sp.run

            def _boom(*a, **k):
                raise AssertionError(
                    "subprocess.run fired under ARO_SKIP_SELFCHECK — "
                    "skip must short-circuit BEFORE version probing")

            _sp.run = _boom
            try:
                with redirect_stderr(io.StringIO()):
                    got = _sc.require_selfcheck(sp)  # no runner inject
                assert got is None
            finally:
                _sp.run = real_run
                del os.environ["ARO_SKIP_SELFCHECK"]

            # terminal gate honors marker (missing → TerminalError)
            # version_runner keeps this path hermetic (no real tool probes).
            del os.environ["ARO_RUNS_ROOT"]  # use a fresh empty root
            os.environ["ARO_RUNS_ROOT"] = str(td / "empty-runs")
            tsp = SimpleNamespace(
                name="term-sc",
                terminal_bench_targets=["mega_bench"],
                terminal_bench_filter=None,
                measure_bin="/fake/reporter",
                icount_epsilon_pct=0.1,
                timeout=1800,
                bench={"pkg": "p"},
                raw={},
            )

            def _runner(cmd, timeout=None):
                body = {"rows": {"row": {"instr_count": 10000}},
                        "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
                return json.dumps(body), "", 0

            try:
                _tm.run_terminal(
                    tsp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
                    rounds=1, floors={}, version_runner=_vrunner)
                assert False, "terminal without marker must hard-error"
            except _tm.TerminalError as e:
                assert "selfcheck" in str(e).lower()

            # with skip_selfcheck=True hermetic path still works + loud warning
            err2 = io.StringIO()
            with redirect_stderr(err2):
                r = _tm.run_terminal(
                    tsp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
                    rounds=1, floors={}, skip_selfcheck=True)
            assert r.verdict == _tm.TERMINAL_UNTOUCHED
            assert "WARNING" in err2.getvalue()
            assert "skip_selfcheck=True" in err2.getvalue()
            # skip-when-absent: no env_fingerprint when selfcheck was skipped
            assert not r.env_fingerprint

            # run_calibrate under skip omits env_fingerprint (no fresh probe)
            with tempfile.TemporaryDirectory() as floord:
                os.environ["ARO_FLOORS_DIR"] = floord
                try:
                    seq = [
                        {"rows": {"r": {"instr_count": 10000}},
                         "meta": {"profile_fingerprint": "fp", "rustc": "r"}},
                        {"rows": {"r": {"instr_count": 10010}},
                         "meta": {"profile_fingerprint": "fp", "rustc": "r"}},
                    ]
                    it = iter(seq)

                    def _cal_r(cmd, timeout=None):
                        return json.dumps(next(it)), "", 0

                    with redirect_stderr(io.StringIO()):
                        payload = _tm.run_calibrate(
                            tsp, "/tmp/checkout-wt", rounds=2,
                            runner=_cal_r, skip_selfcheck=True)
                    assert "env_fingerprint" not in payload["meta"], payload["meta"]
                finally:
                    del os.environ["ARO_FLOORS_DIR"]
        finally:
            os.environ.pop("ARO_RUNS_ROOT", None)
            os.environ.pop("ARO_SKIP_SELFCHECK", None)
            os.environ.pop("ARO_FLOORS_DIR", None)
            _sc.clear_version_cache()
            _sc.set_version_runner(None)
    print("#44d OK: gate integration missing/stale/mismatch/skip/valid")

    # --- records: env_fingerprint additive; absent-field byte-compat ---------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        os.environ["ARO_PERMTREE_DIR"] = str(d / "pt")
        importlib.reload(_pt)
        orig = _les._PATH
        _les._PATH = d / "lessons.jsonl"
        try:
            _les.append("t", "cpu win", "accepted-ir", delta_pct=-2.5,
                        note="Ir gate", ir_delta_pct=-2.5,
                        profile_fingerprint="rustc 1.80|deadbeef",
                        env_fingerprint=fp, backend="codex/gpt-5.2")
            row = json.loads(_les._PATH.read_text().splitlines()[0])
            assert row["env_fingerprint"] == fp
            assert row["profile_fingerprint"] == "rustc 1.80|deadbeef"
            assert row["backend"] == "codex/gpt-5.2"
            # non-icount path: no extra keys
            _les.append("t", "old path", "within-noise", delta_pct=0.1, note="aa")
            row2 = json.loads(_les._PATH.read_text().splitlines()[1])
            assert "env_fingerprint" not in row2
            assert "profile_fingerprint" not in row2
            assert "backend" not in row2

            rec = _pt.record(
                "spec-x", workload="spec-x", fn="hot", base_state="origin",
                verdict="accepted-ir", regime="strict", delta=-2.5,
                env_fingerprint=fp, profile_fingerprint="rustc|abc",
                backend="codex/gpt-5.2")
            assert rec["env_fingerprint"] == fp
            assert rec["backend"] == "codex/gpt-5.2"
            rec2 = _pt.record(
                "spec-x", workload="spec-x", fn="cold", base_state="origin",
                verdict="within-noise", regime="strict", delta=0.0)
            assert "env_fingerprint" not in rec2
            assert "backend" not in rec2

            # terminal record carries env_fingerprint
            res = _tm.TerminalResult(
                verdict=_tm.TERMINAL_CONFIRMED,
                bench_ir_rows={"r": -1.0},
                profile_fingerprint="fp-abc",
                env_fingerprint=fp,
                notes=["ok"],
            )
            ddict = res.to_dict()
            assert ddict["env_fingerprint"] == fp
            # empty env_fingerprint omitted from to_dict (byte-compat)
            res2 = _tm.TerminalResult(
                verdict=_tm.TERMINAL_UNTOUCHED,
                bench_ir_rows={},
                profile_fingerprint="fp-abc",
            )
            assert "env_fingerprint" not in res2.to_dict()
            _tm.record_terminal("spec-x", res, fn="hot")
            les = [json.loads(l) for l in _les._PATH.read_text().splitlines()]
            assert any(r.get("env_fingerprint") == fp for r in les)
        finally:
            _les._PATH = orig
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#44e OK: env_fingerprint/backend additive on lessons/permtree; terminal unchanged")

    # --- --rows row-set integrity (not row-level A/A) ------------------------
    warns = _sc.check_row_set_integrity(
        {"a": 1, "b": 2}, {"a": 0.1, "b": 0.2, "c": 0.3})
    assert any("missing" in w for w in warns)
    warns = _sc.check_row_set_integrity({"a": 1, "b": 2}, {"a": 0.1, "b": 0.2})
    assert any("OK" in w for w in warns)
    warns = _sc.check_row_set_integrity({"a": 1}, {})
    assert any("no calibrated" in w for w in warns)

    # CLI argparse wires selfcheck + --rows
    p = _cli.build_parser()
    a = p.parse_args(["selfcheck", "targets/x.json", "--rows"])
    assert a.cmd == "selfcheck" and a.rows is True
    a2 = p.parse_args(["selfcheck", "targets/x.json"])
    assert a2.rows is False
    print("#44f OK: --rows integrity + CLI wiring")
    print("#44 OK: selfcheck gate + env_fingerprint (T9)")

