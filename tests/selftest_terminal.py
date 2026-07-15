from __future__ import annotations

import json
import tempfile
from pathlib import Path

def case_31():
    """T3: terminal criterion-Ir gate + manifest mergeable + CLI smoke."""
    import importlib
    import os
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import manifest as _mf
    from aro import permtree as _pt
    from aro import lessons as _les
    from aro.attempt import _VERDICT_RANK

    # --- vocabulary: TERMINAL_* are CLOSED, not open, not accept ---
    for v in _tm.ALL_TERMINAL_VERDICTS:
        assert v in _pt._CLOSED_VERDICTS, v
        assert v not in _pt._OPEN_VERDICTS, v
        assert v not in _pt._ACCEPT_VERDICTS, v
        assert v in _VERDICT_RANK, v
    assert _VERDICT_RANK["TERMINAL_CONFIRMED"] > _VERDICT_RANK["TERMINAL_UNTOUCHED"]

    # --- R1 registry equivalence: derived sets/maps == pre-refactor literals ---
    # Frozen pre-refactor literals (do not "fix" these from the registry).
    _PRE_CLOSED = {
        "TERMINAL_CONFIRMED", "TERMINAL_UNTOUCHED", "TERMINAL_REGRESSED",
        "TERMINAL_MIXED", "TERMINAL_TEST_FAILED", "TERMINAL_CONTROL_ANOMALY",
    }
    _PRE_DEAD_ENDS = {
        "TERMINAL_UNTOUCHED", "TERMINAL_REGRESSED", "TERMINAL_MIXED",
        "TERMINAL_TEST_FAILED", "TERMINAL_CONTROL_ANOMALY",
    }
    _PRE_CHART = {
        "TERMINAL_UNTOUCHED": ("#A9B6C2", "terminal Ir untouched (block PR)"),
        "TERMINAL_REGRESSED": ("#DD9580", "terminal Ir regressed"),
        "TERMINAL_MIXED": ("#CBA255", "terminal Ir mixed"),
        "TERMINAL_CONFIRMED": ("#6A9F6A", "terminal Ir confirmed"),
        "TERMINAL_TEST_FAILED": ("#DD9580", "terminal full-suite test failed"),
        "TERMINAL_CONTROL_ANOMALY": (
            "#DD9580", "terminal control-lane composition anomaly"),
    }
    assert _tm.TERMINAL_CLOSED_VERDICTS == frozenset(_PRE_CLOSED)
    assert _tm.TERMINAL_DEAD_END_VERDICTS == frozenset(_PRE_DEAD_ENDS)
    assert _tm.TERMINAL_CHART_STYLES == _PRE_CHART
    assert _tm.ALL_TERMINAL_VERDICTS == frozenset(_PRE_CLOSED)
    # Satellites actually consume the registry (not just re-declare).
    assert _PRE_CLOSED <= set(_pt._CLOSED_VERDICTS)
    from aro.chart import _DOT
    for v, style in _PRE_CHART.items():
        assert _DOT[v] == style, (v, _DOT.get(v), style)
    assert _tm.TERMINAL_CONFIRMED not in _tm.TERMINAL_DEAD_END_VERDICTS
    assert _tm.TERMINAL_VERDICT_META[_tm.TERMINAL_CONFIRMED]["mergeable"] is True
    assert all(
        (not m["mergeable"]) for v, m in _tm.TERMINAL_VERDICT_META.items()
        if v != _tm.TERMINAL_CONFIRMED
    )
    print("#42a OK: TERMINAL_* vocabulary closed / ranked")

    # --- parse measure JSON ---
    doc = _tm.parse_measure_stdout(json.dumps({
        "rows": {
            "mega_bench/sload": {"instr_count": 10000, "ns": 12.3},
            "mega_bench/sstore": {"instr_count": 20000},
        },
        "meta": {"rustc": "rustc 1.80.0", "profile_fingerprint": "fp-abc"},
    }))
    assert doc.rows == {"mega_bench/sload": 10000, "mega_bench/sstore": 20000}
    assert doc.profile_fingerprint == "fp-abc"
    try:
        _tm.parse_measure_stdout("")
        assert False, "empty stdout should hard-error"
    except _tm.TerminalError:
        pass
    try:
        _tm.parse_measure_stdout(json.dumps({
            "rows": {"x": {"ns": 1}}, "meta": {}}))
        assert False, "missing instr_count should hard-error"
    except _tm.TerminalError as e:
        assert "instr_count" in str(e)
    # absent / empty profile_fingerprint must hard-error (never default to '')
    for bad_meta in (
        {},
        {"rustc": "r"},
        {"profile_fingerprint": ""},
        {"profile_fingerprint": "   "},
    ):
        try:
            _tm.parse_measure_stdout(json.dumps({
                "rows": {"x": {"instr_count": 1}}, "meta": bad_meta}))
            assert False, f"empty/missing fingerprint should hard-error: {bad_meta!r}"
        except _tm.TerminalError as e:
            assert "profile_fingerprint" in str(e), e
    try:
        _tm.parse_measure_stdout(json.dumps({
            "rows": {"x": {"instr_count": 1}}}))  # no meta key at all
        assert False, "missing meta should hard-error on fingerprint"
    except _tm.TerminalError as e:
        assert "profile_fingerprint" in str(e)
    print("#42b OK: measure JSON parse + hard errors")

    def _doc(rows, fp="fp-abc"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    # CONFIRMED: one improved, none regressed
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 20000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_CONFIRMED, r
    assert r.bench_ir_rows["a"] == -10.0
    assert "b" not in r.bench_ir_rows  # exact zero Δ omitted
    assert r.profile_fingerprint == "fp-abc"

    # UNTOUCHED: all within ε (exact equal)
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 10000, "b": 20000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_UNTOUCHED, r
    assert r.bench_ir_rows == {}

    # UNTOUCHED: tiny noise inside ε
    r = _tm.judge_terminal(
        _doc({"a": 10000}),
        _doc({"a": 10005}),  # +0.05%
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_UNTOUCHED, r

    # REGRESSED
    r = _tm.judge_terminal(
        _doc({"a": 10000}),
        _doc({"a": 11000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_REGRESSED, r
    assert r.bench_ir_rows["a"] == 10.0

    # MIXED
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 22000}),
        epsilon_pct=0.1)
    assert r.verdict == _tm.TERMINAL_MIXED, r
    assert r.bench_ir_rows["a"] == -10.0 and r.bench_ir_rows["b"] == 10.0

    # fingerprint mismatch → hard error (never a verdict)
    try:
        _tm.judge_terminal(
            _doc({"a": 10000}, fp="fp-1"),
            _doc({"a": 9000}, fp="fp-2"),
            epsilon_pct=0.1)
        assert False, "fingerprint mismatch must hard-error"
    except _tm.TerminalError as e:
        assert "config drift" in str(e) and "fp-1" in str(e)

    # row-set mismatch → hard error
    try:
        _tm.judge_terminal(
            _doc({"a": 10000, "b": 1}),
            _doc({"a": 9000, "c": 1}),
            epsilon_pct=0.1)
        assert False, "row-set mismatch must hard-error"
    except _tm.TerminalError as e:
        assert "row-set mismatch" in str(e)
        assert "dropped" in str(e) and "new" in str(e)
    print("#42c OK: terminal verdicts CONFIRMED/UNTOUCHED/REGRESSED/MIXED + hard errors")

    # --- measure via injectable runner (no real binary) ---
    calls = []

    seen_timeouts = []

    def _runner(cmd, timeout=None):
        calls.append(list(cmd))
        seen_timeouts.append(timeout)
        # Return different docs based on checkout path suffix
        checkout = cmd[cmd.index("--checkout") + 1]
        if "base" in checkout:
            body = {"rows": {"row": {"instr_count": 10000}},
                    "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
        else:
            body = {"rows": {"row": {"instr_count": 9000}},
                    "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
        return json.dumps(body), "", 0

    sp = SimpleNamespace(
        name="term-demo",
        terminal_bench_targets=["mega_bench"],
        terminal_bench_filter=None,
        measure_bin="/fake/mega-bench-reporter",
        icount_epsilon_pct=0.1,
        timeout=1800,
        bench={"pkg": "mega-evm"},
        raw={},
    )
    # rounds=1 keeps this smoke path to a single measure per side; median-of-N
    # is covered in case_32. Empty floors map → default floor (1.0%) still
    # classifies the -10% Δ as CONFIRMED.
    result = _tm.run_terminal(
        sp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
        rounds=1, floors={}, skip_selfcheck=True)
    assert result.verdict == _tm.TERMINAL_CONFIRMED
    assert result.rounds == 1
    assert result.floors_source == "default"
    assert len(calls) == 2
    assert "--instructions" in calls[0] and "mega_bench" in calls[0]
    assert calls[0][0] == "/fake/mega-bench-reporter"
    # default timeout = 4 × spec.timeout, threaded through the runner seam
    assert seen_timeouts == [7200.0, 7200.0], seen_timeouts
    assert _tm.resolve_terminal_timeout(sp) == 7200.0
    # target JSON field terminal_timeout_secs overrides
    sp_to = SimpleNamespace(timeout=1800, terminal_timeout_secs=99, raw={})
    assert _tm.resolve_terminal_timeout(sp_to) == 99.0
    sp_to_raw = SimpleNamespace(timeout=1800, raw={"terminal_timeout_secs": 42})
    assert _tm.resolve_terminal_timeout(sp_to_raw) == 42.0
    # TimeoutExpired from runner → TerminalError (same pattern as valgrind timeout)
    import subprocess as _subprocess

    def _slow(cmd, timeout=None):
        raise _subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    try:
        _tm.measure_checkout(
            "/tmp/base-wt", package="p", bench_targets=["t"],
            measure_bin="/fake/r", timeout=12, runner=_slow)
        assert False, "TimeoutExpired must become TerminalError"
    except _tm.TerminalError as e:
        assert "timed out" in str(e) and "12" in str(e)

    # ARO_MEASURE_BIN wins
    try:
        os.environ["ARO_MEASURE_BIN"] = "/env/reporter"
        assert _tm.resolve_measure_bin(sp) == "/env/reporter"
    finally:
        del os.environ["ARO_MEASURE_BIN"]

    # missing measure_bin → clear TerminalError (not traceback)
    bare = SimpleNamespace(measure_bin=None, raw={}, name="x",
                           terminal_bench_targets=["t"], bench={"pkg": "p"},
                           icount_epsilon_pct=0.1, terminal_bench_filter=None)
    try:
        _tm.resolve_measure_bin(bare)
        assert False
    except _tm.TerminalError as e:
        assert "ARO_MEASURE_BIN" in str(e) or "measure_bin" in str(e)
    print("#42d OK: injectable runner + measure_bin resolution")

    # --- manifest: terminal_required tightens mergeable; no-config byte-identical ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def J(o):
            return json.dumps(o)

        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")

        # (1) no terminal config → legacy shape + mergeable true (byte-identical)
        m0 = _mf.build_manifest(d)
        assert m0["accepted"][0]["mergeable"] is True
        assert "terminal" not in m0["accepted"][0]
        assert "bench_ir_rows" not in m0["accepted"][0]
        assert "terminal" not in m0

        # (2) terminal_required but no result yet → mergeable false
        m1 = _mf.build_manifest(d, terminal_required=True)
        assert m1["accepted"][0]["mergeable"] is False
        assert m1["accepted"][0]["terminal"] is None
        assert m1["accepted"][0]["bench_ir_rows"] == {}

        # (3) tool-written stamp CONFIRMED → mergeable true
        conf = _tm.judge_terminal(
            _doc({"mega_bench/sload": 10000}),
            _doc({"mega_bench/sload": 9680}),  # -3.2%
            epsilon_pct=0.1)
        assert conf.verdict == _tm.TERMINAL_CONFIRMED
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(conf.to_dict(), ensure_ascii=False, indent=1) + "\n")
        m2 = _mf.build_manifest(
            d, terminal_result=conf, terminal_required=True,
            terminal_source=str(tpath))
        assert m2["accepted"][0]["mergeable"] is True
        assert m2["accepted"][0]["terminal"] == "TERMINAL_CONFIRMED"
        assert m2["accepted"][0]["bench_ir_rows"]["mega_bench/sload"] == -3.2
        assert m2["accepted"][0]["profile_fingerprint"] == "fp-abc"
        assert m2["terminal"]["verdict"] == "TERMINAL_CONFIRMED"
        stamp = m2["accepted"][0]["terminal_stamp"]
        assert stamp["verdict"] == "TERMINAL_CONFIRMED"
        assert stamp["source"] == str(tpath)
        assert stamp["sha256"] == _mf.terminal_file_sha256(tpath)

        # (3b) bare terminal_result without on-disk source → no stamp → not mergeable
        m2b = _mf.build_manifest(d, terminal_result=conf, terminal_required=True)
        assert m2b["accepted"][0]["mergeable"] is False
        assert "terminal_stamp" not in m2b["accepted"][0]
        assert _mf.status_flag(m2b["accepted"][0]) == "needs-review (unstamped terminal)"

        # (4) UNTOUCHED → mergeable false even with byte-identical/pass
        unt = _tm.judge_terminal(
            _doc({"a": 10000}), _doc({"a": 10000}), epsilon_pct=0.1)
        assert unt.verdict == _tm.TERMINAL_UNTOUCHED
        m3 = _mf.apply_terminal(dict(m0), unt, terminal_required=True)
        # apply_terminal mutates accepted entries; rebuild for clean apply
        m3 = _mf.build_manifest(d)
        m3 = _mf.apply_terminal(m3, unt, terminal_required=True)
        assert m3["accepted"][0]["mergeable"] is False
        assert m3["accepted"][0]["terminal"] == "TERMINAL_UNTOUCHED"

        # (5) relaxed/pass-risk stays non-mergeable even under stamped CONFIRMED
        conf_stamp = {
            "verdict": "TERMINAL_CONFIRMED",
            "source": str(tpath),
            "sha256": stamp["sha256"],
        }
        assert _mf.is_mergeable("relaxed", "pass",
                                terminal="TERMINAL_CONFIRMED",
                                terminal_required=True,
                                terminal_stamp=conf_stamp) is False
        assert _mf.is_mergeable("byte-identical", "pass-risk",
                                terminal="TERMINAL_CONFIRMED",
                                terminal_required=True,
                                terminal_stamp=conf_stamp) is False
        assert _mf.is_mergeable("byte-identical", "pass",
                                terminal="TERMINAL_CONFIRMED",
                                terminal_required=True,
                                terminal_stamp=conf_stamp) is True
        # bare terminal string without stamp is inert for mergeability
        assert _mf.is_mergeable("byte-identical", "pass",
                                terminal="TERMINAL_CONFIRMED",
                                terminal_required=True) is False
        # no terminal config: stamp/terminal ignored
        assert _mf.is_mergeable("byte-identical", "pass",
                                terminal="TERMINAL_UNTOUCHED",
                                terminal_required=False) is True
    print("#42e OK: manifest mergeable terminal gate + backward-compatible shape")

    # --- record_terminal → lessons + permtree carry fingerprint ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        os.environ["ARO_PERMTREE_DIR"] = str(d / "pt")
        importlib.reload(_pt)
        orig = _les._PATH
        _les._PATH = d / "lessons.jsonl"
        try:
            res = _tm.TerminalResult(
                verdict=_tm.TERMINAL_UNTOUCHED,
                bench_ir_rows={},
                profile_fingerprint="rustc 1.80|deadbeef",
                notes=["verdict: TERMINAL_UNTOUCHED — probe-vs-bench divergence"],
            )
            _tm.record_terminal("mega-evm-v2", res, fn="sload",
                                hypothesis="hoist sload oracle")
            les = json.loads(_les._PATH.read_text().splitlines()[0])
            assert les["verdict"] == "TERMINAL_UNTOUCHED"
            assert les["profile_fingerprint"] == "rustc 1.80|deadbeef"
            ns = _pt.nodes("mega-evm-v2")
            key = _pt.node_key("mega-evm-v2", "sload", "origin")
            assert ns[key]["verdict"] == "TERMINAL_UNTOUCHED"
            assert ns[key]["profile_fingerprint"] == "rustc 1.80|deadbeef"
        finally:
            _les._PATH = orig
            del os.environ["ARO_PERMTREE_DIR"]
            importlib.reload(_pt)
    print("#42f OK: terminal lessons/permtree record with fingerprint")

    # --- CLI smoke: --list without measure binary; missing bin clear error ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # fixture spec WITHOUT measure_bin, WITH terminal targets; missing repo path
        missing_repo = d / "no-repo"
        spec_path = d / "term-smoke.json"
        spec_path.write_text(json.dumps({
            "name": "term-smoke",
            "target_repo": {"path": str(missing_repo)},
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "metric": "ns_per_call",
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "terminal_bench_targets": ["mega_bench"],
            "icount_epsilon_pct": 0.1,
        }))
        import io
        from contextlib import redirect_stdout, redirect_stderr
        buf = io.StringIO()
        with redirect_stdout(buf):
            _tm.cli(SimpleNamespace(
                spec=str(spec_path), list=True, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=None, hypothesis=None, events_ref=None))
        out = buf.getvalue()
        assert "terminal config for term-smoke" in out, out
        assert "mega_bench" in out
        assert "UNSET" in out  # measure_bin unset is reported, not a crash
        assert "gate active:            True" in out

        # measure path without bin → exit 2 with clear message.
        # Skip selfcheck so this smoke still exercises the measure_bin error
        # (selfcheck hard-error is covered in case_33).
        err = io.StringIO()
        os.environ["ARO_SKIP_SELFCHECK"] = "1"
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                _tm.cli(SimpleNamespace(
                    spec=str(spec_path), list=False, dry_run=False,
                    baseline=str(d / "b"), candidate=str(d / "c"),
                    out=None, record=False, fn=None, update_manifest=None,
                    hypothesis=None, events_ref=None))
            assert False, "missing measure_bin must SystemExit"
        except SystemExit as se:
            assert se.code == 2
        finally:
            del os.environ["ARO_SKIP_SELFCHECK"]
        assert "measure binary unset" in err.getvalue() or \
               "ARO_MEASURE_BIN" in err.getvalue()

        # mocked runner end-to-end via run_terminal already covered; CLI with
        # env measure_bin + custom runner is module-level (cli uses subprocess).
        # Exercise build_measure_cmd shape:
        cmd = _tm.build_measure_cmd(
            "/bin/reporter", "/wt", package="mega-evm",
            bench_targets=["mega_bench", "other"], bench_filter="sload")
        assert cmd[:2] == ["/bin/reporter", "measure"]
        assert "--instructions" in cmd
        assert cmd.count("--bench-target") == 2
        assert "--bench-filter" in cmd and "sload" in cmd
    print("#42g OK: CLI --list / missing-bin message / measure cmd shape")
    print("#42 OK: terminal criterion-Ir gate (T3)")

def case_32():
    """T8: per-row terminal floors + median-of-N sampling (hermetic)."""
    import io
    import os
    from contextlib import redirect_stderr, redirect_stdout
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import cli as _cli

    def _doc(rows, fp="fp-abc"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    # --- calibration math: max pairwise |Δ%| × 2, clamped to ε ---------------
    # values 10000, 10020 → pairwise = 20/10000*100 = 0.2% → ×2 = 0.4%
    assert abs(_tm.pairwise_abs_pct(10000, 10020) - 0.2) < 1e-9
    assert abs(_tm.max_pairwise_delta_pct([10000, 10020, 9990]) -
               _tm.pairwise_abs_pct(10020, 9990)) < 1e-9
    fl = _tm.calibrate_row_floor([10000, 10020], min_floor_pct=0.1)
    assert abs(fl - 0.4) < 1e-9, fl
    # tiny noise → clamp to ε
    fl_lo = _tm.calibrate_row_floor([10000, 10001], min_floor_pct=0.1)
    assert abs(fl_lo - 0.1) < 1e-9, fl_lo  # 0.01% × 2 = 0.02 → clamp 0.1
    # single value → min floor
    assert _tm.calibrate_row_floor([10000], min_floor_pct=0.1) == 0.1

    d1 = _doc({"a": 10000, "b": 20000})
    d2 = _doc({"a": 10040, "b": 20000})  # a: 0.4% pair → floor 0.8%; b: 0
    d3 = _doc({"a": 9990, "b": 20100})
    floors = _tm.compute_floors_from_docs([d1, d2, d3], min_floor_pct=0.1)
    assert floors["a"] == _tm.calibrate_row_floor(
        [10000, 10040, 9990], min_floor_pct=0.1)
    assert floors["b"] == _tm.calibrate_row_floor(
        [20000, 20000, 20100], min_floor_pct=0.1)
    assert floors["b"] >= 0.1
    print("#43a OK: calibration math (max pairwise ×2, ε clamp)")

    # --- floors file write / load + meta + staleness warnings ----------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_FLOORS_DIR"] = str(td)
        try:
            meta = {
                "calibrated_at": "2020-01-01T00:00:00Z",  # stale
                "rounds": 3,
                "checkout_describe": "deadbeef",
                "measure_bin": "/fake/reporter",
                "rustc": "rustc 0.0.0-stale",
            }
            dest = _tm.write_floors("demo", {"row/x": 0.5, "row/y": 0.1}, meta=meta)
            assert dest == td / "demo.json"
            loaded, meta_l, warns = _tm.load_floors("demo")
            assert loaded == {"row/x": 0.5, "row/y": 0.1}
            assert meta_l["rounds"] == 3
            assert meta_l["checkout_describe"] == "deadbeef"
            # age + rustc mismatch (when rustc is present) → warnings, not errors
            assert any("old" in w or "stale" in w.lower() or "calibrated_at" in w
                       for w in warns), warns
            # missing file → empty + warning
            empty, _, w2 = _tm.load_floors("no-such-spec")
            assert empty == {}
            assert any("no calibrated file" in w for w in w2)

            # Non-finite / non-positive floors must be skipped (fallback to
            # default_floor_pct) with a warning naming the row + bad value.
            # Accepting NaN would make both comparisons False → silent UNTOUCHED
            # on a real regression; negative floors invert classification.
            bad_payload = {
                "meta": {
                    "calibrated_at": "2026-01-01T00:00:00Z",
                    "rounds": 3,
                    "rustc": "rustc 1.80.0",
                },
                "floors": {
                    "ok": 0.5,
                    "nan_row": float("nan"),
                    "inf_row": float("inf"),
                    "neg_row": -1.0,
                    "zero_row": 0.0,
                },
            }
            (td / "bad.json").write_text(json.dumps(bad_payload) + "\n")
            loaded_bad, _, warns_bad = _tm.load_floors("bad")
            assert loaded_bad == {"ok": 0.5}, loaded_bad
            for bad_key in ("nan_row", "inf_row", "neg_row", "zero_row"):
                assert bad_key not in loaded_bad
                assert any(bad_key in w for w in warns_bad), (bad_key, warns_bad)
            # Fallback classification: +5% with default floor 1.0% → REGRESSED.
            # (NaN floor would have classified the same Δ as UNTOUCHED.)
            r_fb = _tm.judge_terminal(
                _doc({"nan_row": 10000}), _doc({"nan_row": 10500}),
                epsilon_pct=0.1, floors=loaded_bad, default_floor_pct=1.0,
                floors_source="default")
            assert r_fb.verdict == _tm.TERMINAL_REGRESSED, r_fb
            assert r_fb.rows[0].floor_pct == 1.0
            assert r_fb.rows[0].status == "regressed"
        finally:
            del os.environ["ARO_FLOORS_DIR"]
    print("#43b OK: floors file write/load + staleness warnings")

    # --- floor-aware classification ------------------------------------------
    # floors all = ε → backward-comparable with legacy ε threshold
    r = _tm.judge_terminal(
        _doc({"a": 10000}), _doc({"a": 10005}),  # +0.05%
        epsilon_pct=0.1, floors={"a": 0.1}, floors_source="calibrated")
    assert r.verdict == _tm.TERMINAL_UNTOUCHED
    assert r.floors_source == "calibrated"
    assert r.rows[0].floor_pct == 0.1

    # Δ = +0.5% with floor 1.0 → untouched; with floor 0.1 → regressed
    r = _tm.judge_terminal(
        _doc({"a": 10000}), _doc({"a": 10050}),
        epsilon_pct=0.1, floors={}, default_floor_pct=1.0, floors_source="default")
    assert r.verdict == _tm.TERMINAL_UNTOUCHED, r
    r = _tm.judge_terminal(
        _doc({"a": 10000}), _doc({"a": 10050}),
        epsilon_pct=0.1, floors={"a": 0.1}, floors_source="calibrated")
    assert r.verdict == _tm.TERMINAL_REGRESSED, r

    # mixed floors: calibrated a (0.1) + default b (1.0)
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9900, "b": 20100}),  # a -1%, b +0.5%
        epsilon_pct=0.1,
        floors={"a": 0.1},
        default_floor_pct=1.0,
        floors_source="mixed",
    )
    assert r.verdict == _tm.TERMINAL_CONFIRMED, r  # a improved, b within default
    assert r.floors_source == "mixed"
    assert r.rows[0].status == "improved" and r.rows[1].status == "untouched"

    # calibrated tiny floor still yields UNTOUCHED for equal rows
    r = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 10000, "b": 20000}),
        epsilon_pct=0.1, floors={"a": 0.1, "b": 0.1}, floors_source="calibrated")
    assert r.verdict == _tm.TERMINAL_UNTOUCHED
    # payload carries rounds + floors_source
    d = r.to_dict()
    assert d["floors_source"] == "calibrated"
    assert "rounds" in d and "floor_pct" in d["rows"][0]
    print("#43c OK: floor-aware classification (calibrated/default/mixed)")

    # --- median-of-N (odd + even) --------------------------------------------
    # odd: median is middle element
    assert _tm.median_ir([9000, 10000, 11000]) == 10000
    # even: average of two middle, rounded
    assert _tm.median_ir([9000, 10000, 11000, 12000]) == 10500

    med = _tm.median_measure_docs([
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 21000}),
        _doc({"a": 11000, "b": 19000}),
    ])
    assert med.rows["a"] == 10000
    assert med.rows["b"] == 20000
    assert med.profile_fingerprint == "fp-abc"

    # fingerprint drift across rounds → hard error
    try:
        _tm.median_measure_docs([
            _doc({"a": 1}, fp="fp-1"),
            _doc({"a": 1}, fp="fp-2"),
        ])
        assert False
    except _tm.TerminalError as e:
        assert "config drift" in str(e)

    # row present in some rounds but missing in one of the SAME side →
    # row-set-mismatch TerminalError before any median is computed
    # (symmetric to fingerprint-drift above)
    try:
        _tm.median_measure_docs([
            _doc({"a": 10000, "b": 20000}),
            _doc({"a": 10010}),            # b dropped mid-side
            _doc({"a": 9990, "b": 20100}),
        ])
        assert False, "row-set mismatch across rounds of same side must hard-error"
    except _tm.TerminalError as e:
        assert "row-set mismatch" in str(e), e
        assert "dropped" in str(e) and "b" in str(e)

    # run_terminal measures rounds times per side
    calls = []

    def _runner(cmd, timeout=None):
        calls.append(list(cmd))
        checkout = cmd[cmd.index("--checkout") + 1]
        # Vary Ir slightly across calls; median of three still yields a clear win.
        side_calls = [c for c in calls if c[c.index("--checkout") + 1] == checkout]
        i = len(side_calls)  # 1-based after append
        if "base" in checkout:
            # 10000, 10010, 9990 → median 10000
            ir = {1: 10000, 2: 10010, 3: 9990, 4: 10000}.get(i, 10000)
        else:
            # 9000, 9010, 8990 → median 9000 → Δ = -10%
            ir = {1: 9000, 2: 9010, 3: 8990, 4: 9000}.get(i, 9000)
        body = {"rows": {"row": {"instr_count": ir}},
                "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
        return json.dumps(body), "", 0

    sp = SimpleNamespace(
        name="term-med",
        terminal_bench_targets=["mega_bench"],
        terminal_bench_filter=None,
        measure_bin="/fake/reporter",
        icount_epsilon_pct=0.1,
        terminal_default_floor_pct=1.0,
        timeout=1800,
        bench={"pkg": "mega-evm"},
        raw={},
    )
    result = _tm.run_terminal(
        sp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
        rounds=3, floors={"row": 0.1}, skip_selfcheck=True)
    assert result.verdict == _tm.TERMINAL_CONFIRMED, result
    assert result.rounds == 3
    assert result.floors_source == "calibrated"
    assert len(calls) == 6  # 3 base + 3 cand
    assert result.bench_ir_rows["row"] == -10.0
    # even rounds via measure_checkout_rounds
    calls.clear()
    result4 = _tm.run_terminal(
        sp, "/tmp/base-wt", "/tmp/cand-wt", runner=_runner,
        rounds=4, floors={"row": 0.1}, skip_selfcheck=True)
    assert result4.rounds == 4
    assert len(calls) == 8
    assert result4.verdict == _tm.TERMINAL_CONFIRMED

    # env ARO_TERMINAL_ROUNDS wins
    try:
        os.environ["ARO_TERMINAL_ROUNDS"] = "2"
        assert _tm.resolve_terminal_rounds(sp) == 2
    finally:
        del os.environ["ARO_TERMINAL_ROUNDS"]
    assert _tm.resolve_terminal_rounds(sp) == 3  # default
    sp_r = SimpleNamespace(terminal_measure_rounds=5, raw={})
    assert _tm.resolve_terminal_rounds(sp_r) == 5
    print("#43d OK: median-of-N (odd/even) + rounds resolution")

    # --- calibrate via injectable runner + CLI dry-run -----------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_FLOORS_DIR"] = str(td)
        try:
            seq = [
                {"rows": {"r1": {"instr_count": 10000}, "r2": {"instr_count": 50000}},
                 "meta": {"profile_fingerprint": "fp", "rustc": "rustc 1.80"}},
                {"rows": {"r1": {"instr_count": 10020}, "r2": {"instr_count": 50000}},
                 "meta": {"profile_fingerprint": "fp", "rustc": "rustc 1.80"}},
                {"rows": {"r1": {"instr_count": 9990}, "r2": {"instr_count": 50100}},
                 "meta": {"profile_fingerprint": "fp", "rustc": "rustc 1.80"}},
            ]
            it = iter(seq)

            def _cal_runner(cmd, timeout=None):
                body = next(it)
                return json.dumps(body), "", 0

            spc = SimpleNamespace(
                name="cal-demo",
                terminal_bench_targets=["mega_bench"],
                terminal_bench_filter=None,
                measure_bin="/fake/reporter",
                icount_epsilon_pct=0.1,
                timeout=1800,
                bench={"pkg": "p"},
                raw={},
            )
            payload = _tm.run_calibrate(
                spc, "/tmp/checkout-wt", rounds=3, runner=_cal_runner,
                skip_selfcheck=True)
            assert Path(payload["path"]).is_file()
            assert payload["meta"]["rounds"] == 3
            assert "calibrated_at" in payload["meta"]
            assert "measure_bin" in payload["meta"]
            assert "r1" in payload["floors"] and "r2" in payload["floors"]
            assert payload["floors"]["r1"] == _tm.calibrate_row_floor(
                [10000, 10020, 9990], min_floor_pct=0.1)
            # re-load via load_floors
            fl, meta, _ = _tm.load_floors("cal-demo")
            assert fl["r1"] == payload["floors"]["r1"]
            assert meta["rounds"] == 3
        finally:
            del os.environ["ARO_FLOORS_DIR"]

    # CLI dry-run: clean without measure binary
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        spec_path = d / "cal.json"
        spec_path.write_text(json.dumps({
            "name": "cal-smoke",
            "target_repo": {"path": str(d / "no-repo")},
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "metric": "ns_per_call",
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "terminal_bench_targets": ["mega_bench"],
            "icount_epsilon_pct": 0.1,
        }))
        buf = io.StringIO()
        with redirect_stdout(buf):
            _tm.calibrate_cli(SimpleNamespace(
                spec=str(spec_path), checkout=str(d / "wt"),
                rounds=4, dry_run=True, measure_bin=None))
        out = buf.getvalue()
        assert "terminal-calibrate dry-run" in out, out
        assert "rounds:    4" in out
        assert "would write floors" in out
        assert "MEASURE_BIN" in out or "UNSET" in out

        # missing measure_bin on real calibrate path → clear error, not traceback.
        # Skip selfcheck so this smoke still exercises the measure_bin error
        # (selfcheck hard-error is covered in case_33).
        err = io.StringIO()
        os.environ["ARO_SKIP_SELFCHECK"] = "1"
        try:
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                _tm.calibrate_cli(SimpleNamespace(
                    spec=str(spec_path), checkout=str(d / "wt"),
                    rounds=2, dry_run=False, measure_bin=None))
            assert False, "missing measure_bin must SystemExit"
        except SystemExit as se:
            assert se.code == 2
        finally:
            del os.environ["ARO_SKIP_SELFCHECK"]
        assert "measure binary unset" in err.getvalue() or \
               "ARO_MEASURE_BIN" in err.getvalue()

        # argparse wires terminal-calibrate
        p = _cli.build_parser()
        a = p.parse_args([
            "terminal-calibrate", str(spec_path),
            "--checkout", str(d / "wt"), "--rounds", "3", "--dry-run",
        ])
        assert a.cmd == "terminal-calibrate"
        assert a.rounds == 3 and a.dry_run is True
    print("#43e OK: calibrate runner + CLI dry-run / missing-bin")
    print("#43 OK: terminal floors + median-of-N (T8)")

def case_35():
    """T13b: terminal-gate full-suite correctness tier (test_full).

    Hermetic — injects test_full_runner + measure runner; never spawns cargo.
    (a) absent test_full → byte-identical (no runner invocation)
    (b) declared + exit 0 → measure proceeds, verdict unaffected
    (c) declared + exit 1 → TERMINAL_TEST_FAILED, no measure, output tail
    (d) verdict survives terminal.json round-trip; apply_terminal keeps
        mergeable=false for every entry
    """
    print("=== case 35: terminal test_full correctness tier ===")
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import manifest as _mf

    assert _tm.TERMINAL_TEST_FAILED in _tm.ALL_TERMINAL_VERDICTS
    from aro import permtree as _pt
    from aro.attempt import _VERDICT_RANK
    assert _tm.TERMINAL_TEST_FAILED in _pt._CLOSED_VERDICTS
    assert _tm.TERMINAL_TEST_FAILED in _VERDICT_RANK

    def _mk_spec(**raw_extra):
        raw = dict(raw_extra)
        return SimpleNamespace(
            name="term-tf",
            terminal_bench_targets=["mega_bench"],
            terminal_bench_filter=None,
            measure_bin="/fake/reporter",
            icount_epsilon_pct=0.1,
            timeout=1800,
            bench={"pkg": "mega-evm"},
            raw=raw,
        )

    def _measure_runner_factory(calls):
        def _runner(cmd, timeout=None):
            calls.append(list(cmd))
            checkout = cmd[cmd.index("--checkout") + 1]
            if "base" in checkout:
                body = {"rows": {"row": {"instr_count": 10000}},
                        "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
            else:
                body = {"rows": {"row": {"instr_count": 9000}},
                        "meta": {"profile_fingerprint": "fp-x", "rustc": "r"}}
            return json.dumps(body), "", 0
        return _runner

    # (a) absent test_full → no runner invocation; measure still runs
    measure_calls = []
    tf_calls = []

    def _tf_a(cmd, *, cwd, timeout=None):
        tf_calls.append((list(cmd), str(cwd), timeout))
        return "", "", 0

    sp_a = _mk_spec()  # no correctness_oracle.test_full
    assert _tm.resolve_test_full(sp_a) is None
    r_a = _tm.run_terminal(
        sp_a, "/tmp/base-wt", "/tmp/cand-wt",
        runner=_measure_runner_factory(measure_calls),
        test_full_runner=_tf_a,
        rounds=1, floors={}, skip_selfcheck=True)
    assert r_a.verdict == _tm.TERMINAL_CONFIRMED, r_a
    assert tf_calls == [], f"absent test_full must not invoke runner: {tf_calls}"
    assert len(measure_calls) == 2
    print("#46a OK: absent test_full → no runner, measure proceeds")

    # (b) declared + exit 0 → measure proceeds, verdict unaffected
    measure_calls.clear()
    tf_calls.clear()

    def _tf_ok(cmd, *, cwd, timeout=None):
        tf_calls.append((list(cmd), str(cwd), timeout))
        return "test result: ok. 12 passed\n", "", 0

    sp_b = _mk_spec(correctness_oracle={
        "build": ["cargo", "build"],
        "test": ["cargo", "test", "--lib"],
        "test_full": ["cargo", "test", "--release", "-p", "mega-evm"],
    })
    assert _tm.resolve_test_full(sp_b) == [
        "cargo", "test", "--release", "-p", "mega-evm"]
    assert _tm.resolve_test_full_timeout(sp_b) == 1800.0
    r_b = _tm.run_terminal(
        sp_b, "/tmp/base-wt", "/tmp/cand-wt",
        runner=_measure_runner_factory(measure_calls),
        test_full_runner=_tf_ok,
        rounds=1, floors={}, skip_selfcheck=True)
    assert r_b.verdict == _tm.TERMINAL_CONFIRMED, r_b
    assert len(tf_calls) == 1
    assert tf_calls[0][0] == ["cargo", "test", "--release", "-p", "mega-evm"]
    assert "cand" in tf_calls[0][1]  # candidate_dir only
    assert tf_calls[0][2] == 1800.0
    assert len(measure_calls) == 2
    print("#46b OK: test_full exit 0 → measure proceeds, CONFIRMED")

    # (c) declared + exit 1 → TERMINAL_TEST_FAILED, no measure, output tail
    measure_calls.clear()
    tf_calls.clear()
    long_out = ("ok line\n" * 50) + ("FAIL: semantics_diff\n" * 100)
    assert len(long_out) > 2000

    def _tf_fail(cmd, *, cwd, timeout=None):
        tf_calls.append((list(cmd), str(cwd), timeout))
        return long_out, "error: test failed\n", 1

    r_c = _tm.run_terminal(
        sp_b, "/tmp/base-wt", "/tmp/cand-wt",
        runner=_measure_runner_factory(measure_calls),
        test_full_runner=_tf_fail,
        rounds=1, floors={}, skip_selfcheck=True)
    assert r_c.verdict == _tm.TERMINAL_TEST_FAILED, r_c
    assert measure_calls == [], f"must not measure after test fail: {measure_calls}"
    assert len(tf_calls) == 1
    assert any("TERMINAL_TEST_FAILED" in n for n in r_c.notes)
    # last ~2000 chars of combined output are retained
    joined = "\n".join(r_c.notes)
    assert "FAIL: semantics_diff" in joined
    assert len(joined) < len(long_out) + 500  # tail capped, not full dump
    assert r_c.bench_ir_rows == {}
    assert r_c.rounds == 0
    print("#46c OK: test_full exit 1 → TERMINAL_TEST_FAILED, no measure, tail kept")

    # (d) terminal.json round-trip + apply_terminal keeps mergeable=false
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def J(o):
            return json.dumps(o)

        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
            # second accepted entry so "every entry" is non-trivial
            {"event": "attempt_started", "run_id": "R", "fn": "sstore",
             "regime": "byte-identical", "files": ["crates/x/src/c.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-1",
             "hypothesis": "inline"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-1", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-1",
             "deltas": [{"metric": "ns", "delta_pct": -2.0, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-1"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        for cid, fname in (("agent-r0-0", "b.rs"), ("agent-r0-1", "c.rs")):
            pd = d / "a1" / "patches"
            pd.mkdir(parents=True, exist_ok=True)
            (pd / f"{cid}.txt").write_text(
                f"--- edit 1 ---\npath: crates/x/src/{fname}\n"
                "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")

        # Write terminal.json via TerminalResult.to_dict (CLI --out path)
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(r_c.to_dict(), ensure_ascii=False, indent=1) + "\n")
        loaded = json.loads(tpath.read_text())
        assert loaded["verdict"] == "TERMINAL_TEST_FAILED"
        assert any("TERMINAL_TEST_FAILED" in n for n in loaded["notes"])
        # notes carry the output tail through the round-trip
        assert "FAIL: semantics_diff" in "\n".join(loaded["notes"])

        m = _mf.build_manifest(d, terminal_required=True)
        assert all(a["mergeable"] is False for a in m["accepted"])
        m = _mf.apply_terminal(m, loaded, terminal_required=True)
        assert m["terminal"]["verdict"] == "TERMINAL_TEST_FAILED"
        assert len(m["accepted"]) >= 2
        for a in m["accepted"]:
            assert a["terminal"] == "TERMINAL_TEST_FAILED", a
            assert a["mergeable"] is False, a
        # is_mergeable itself rejects the new verdict
        assert _mf.is_mergeable(
            "byte-identical", "pass",
            terminal=_tm.TERMINAL_TEST_FAILED,
            terminal_required=True) is False

        # test_full_timeout_secs override
        sp_to = _mk_spec(
            correctness_oracle={"test_full": ["true"]},
            test_full_timeout_secs=99)
        assert _tm.resolve_test_full_timeout(sp_to) == 99.0
    print("#46d OK: terminal.json round-trip + apply_terminal mergeable=false")
    print("case 35 OK")

def case_36():
    """T14: lane-aware terminal verdict + offline re-judge.

    Hermetic — pure judge_terminal / rejudge_terminal_doc / apply_terminal.
    (a) mixed subject+control within bound → CONFIRMED, controls control-ok
    (b) control beyond bound → TERMINAL_CONTROL_ANOMALY even if subjects improved
    (c) no control_lanes → byte-identical legacy statuses/verdict
    (d) segment-exact matching (not substring)
    (e) re-judge round-trip: verdict flips, input unmodified, note appended
    (f) apply_terminal keeps mergeable=false under TERMINAL_CONTROL_ANOMALY
    """
    print("=== case 36: lane-aware terminal + rejudge ===")
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import manifest as _mf
    from aro import permtree as _pt
    from aro.attempt import _VERDICT_RANK

    assert _tm.TERMINAL_CONTROL_ANOMALY in _tm.ALL_TERMINAL_VERDICTS
    assert _tm.TERMINAL_CONTROL_ANOMALY in _pt._CLOSED_VERDICTS
    assert _tm.TERMINAL_CONTROL_ANOMALY in _VERDICT_RANK

    CONTROL = ["revm_pinned", "revm_latest", "op_revm_pinned", "op_revm_latest"]
    BOUND = 2.0

    def _doc(rows, fp="fp-lane"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    # (d) segment matching first — pure function, no judge
    assert _tm.is_control_row(
        "log_opcodes/op_revm_latest/log4_32b", CONTROL) is True
    assert _tm.is_control_row(
        "system_contract_100x/rex4/limit_control", CONTROL) is False
    assert _tm.is_control_row(
        "revm_pinned_x/rex5/case", CONTROL) is False  # not exact segment
    assert _tm.is_control_row(
        "group/revm_pinned/case", CONTROL) is True
    assert _tm.is_control_row("a/b/c", []) is False
    print("#47d OK: segment-exact control-lane matching")

    # (a) subject improved + control within bound → CONFIRMED
    r_a = _tm.judge_terminal(
        _doc({
            "log_opcodes/rex4/log4_32b": 10000,
            "log_opcodes/op_revm_latest/log4_32b": 20000,
            "system_contract_100x/rex5/case": 5000,
            "group/revm_pinned/case": 8000,
        }),
        _doc({
            "log_opcodes/rex4/log4_32b": 9000,       # -10% subject improved
            "log_opcodes/op_revm_latest/log4_32b": 20100,  # +0.5% control-ok
            "system_contract_100x/rex5/case": 5000,  # 0 subject untouched
            "group/revm_pinned/case": 8100,          # +1.25% control-ok
        }),
        epsilon_pct=0.1,
        control_lanes=CONTROL,
        control_composition_bound_pct=BOUND,
    )
    assert r_a.verdict == _tm.TERMINAL_CONFIRMED, r_a
    by_key = {rd.row_key: rd for rd in r_a.rows}
    assert by_key["log_opcodes/rex4/log4_32b"].status == "improved"
    assert by_key["log_opcodes/op_revm_latest/log4_32b"].status == "control-ok"
    assert by_key["group/revm_pinned/case"].status == "control-ok"
    assert by_key["system_contract_100x/rex5/case"].status == "untouched"
    # Counters exclude controls: only the one improved subject
    assert any("improved=1" in n for n in r_a.notes), r_a.notes
    assert any("regressed=0" in n for n in r_a.notes), r_a.notes
    assert any("control rows:" in n for n in r_a.notes)
    assert any("exceeded=0" in n for n in r_a.notes)
    print("#47a OK: subject improved + control-ok → CONFIRMED")

    # (b) control beyond bound → CONTROL_ANOMALY even when subjects all improved
    r_b = _tm.judge_terminal(
        _doc({
            "log_opcodes/rex4/log4_32b": 10000,
            "log_opcodes/op_revm_latest/log4_32b": 20000,
        }),
        _doc({
            "log_opcodes/rex4/log4_32b": 9000,        # -10% subject improved
            "log_opcodes/op_revm_latest/log4_32b": 21000,  # +5% > 2% bound
        }),
        epsilon_pct=0.1,
        control_lanes=CONTROL,
        control_composition_bound_pct=BOUND,
    )
    assert r_b.verdict == _tm.TERMINAL_CONTROL_ANOMALY, r_b
    by_b = {rd.row_key: rd for rd in r_b.rows}
    assert by_b["log_opcodes/rex4/log4_32b"].status == "improved"
    assert by_b["log_opcodes/op_revm_latest/log4_32b"].status == "control-anomaly"
    assert any("exceeded=1" in n for n in r_b.notes)
    print("#47b OK: control-anomaly → TERMINAL_CONTROL_ANOMALY (fail-closed)")

    # (c) no control_lanes → byte-identical legacy verdict + row statuses
    base_rows = {
        "log_opcodes/op_revm_latest/log4_32b": 20000,
        "log_opcodes/rex4/log4_32b": 10000,
    }
    cand_rows = {
        "log_opcodes/op_revm_latest/log4_32b": 20100,  # +0.5% → regressed under 0.1
        "log_opcodes/rex4/log4_32b": 9000,             # -10% improved
    }
    r_legacy = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1)
    r_empty = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1,
        control_lanes=None)
    r_empty2 = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1,
        control_lanes=[])
    assert r_legacy.verdict == _tm.TERMINAL_MIXED, r_legacy
    assert r_empty.verdict == r_legacy.verdict
    assert r_empty2.verdict == r_legacy.verdict
    assert [rd.status for rd in r_legacy.rows] == [rd.status for rd in r_empty.rows]
    assert [rd.status for rd in r_legacy.rows] == [rd.status for rd in r_empty2.rows]
    assert [rd.delta_pct for rd in r_legacy.rows] == [
        rd.delta_pct for rd in r_empty.rows]
    # Same inputs WITH control_lanes flip the op_revm row out of subject counters
    r_lane = _tm.judge_terminal(
        _doc(base_rows), _doc(cand_rows), epsilon_pct=0.1,
        control_lanes=CONTROL, control_composition_bound_pct=BOUND)
    assert r_lane.verdict == _tm.TERMINAL_CONFIRMED, r_lane  # only subject improved
    assert {rd.row_key: rd.status for rd in r_lane.rows}[
        "log_opcodes/op_revm_latest/log4_32b"] == "control-ok"
    print("#47c OK: absent control_lanes → byte-identical legacy verdict/statuses")

    # (e) re-judge round-trip: subject MIXED → CONFIRMED under wider floors.
    # Lane-aware verify (control_lanes provided) requires stored class to match
    # row_key derivation, so the input uses subject-only keys (no control
    # segments). Re-adjudication with floors absorbs the regression.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        subj_base = {"rex4/log4_32b": 10000, "rex5/case": 20000}
        subj_cand = {"rex4/log4_32b": 9000, "rex5/case": 22000}  # -10% / +10%
        old = _tm.judge_terminal(
            _doc(subj_base), _doc(subj_cand), epsilon_pct=0.1)
        assert old.verdict == _tm.TERMINAL_MIXED
        old.env_fingerprint = "codspeed=1;rustc=1.80"
        in_path = d / "terminal.json"
        in_text = json.dumps(old.to_dict(), ensure_ascii=False, indent=1) + "\n"
        in_path.write_text(in_text)

        rejudged = _tm.rejudge_terminal_doc(
            json.loads(in_path.read_text()),
            epsilon_pct=0.1,
            floors={"rex4/log4_32b": 0.1, "rex5/case": 50.0},
            default_floor_pct=0.1,
            control_lanes=CONTROL,
            control_composition_bound_pct=BOUND,
        )
        assert rejudged.verdict == _tm.TERMINAL_CONFIRMED, rejudged
        assert rejudged.profile_fingerprint == "fp-lane"
        assert rejudged.env_fingerprint == "codspeed=1;rustc=1.80"
        assert rejudged.rounds == old.rounds
        assert any("re-judged offline" in n for n in rejudged.notes)
        assert any("control_lanes=" in n for n in rejudged.notes)

        # Simulate CLI write path: never overwrite input
        out_path = Path(str(in_path) + ".rejudged.json")
        out_path.write_text(
            json.dumps(rejudged.to_dict(), ensure_ascii=False, indent=1) + "\n")
        assert in_path.read_text() == in_text  # input unmodified
        assert out_path.is_file()
        loaded = json.loads(out_path.read_text())
        assert loaded["verdict"] == "TERMINAL_CONFIRMED"
        assert any("re-judged offline" in n for n in loaded["notes"])
    print("#47e OK: rejudge round-trip flips MIXED→CONFIRMED, input intact")

    # (f) apply_terminal / is_mergeable keep mergeable=false
    assert _mf.is_mergeable(
        "byte-identical", "pass",
        terminal=_tm.TERMINAL_CONTROL_ANOMALY,
        terminal_required=True) is False
    m = {
        "accepted": [{
            "id": "c0", "regime": "byte-identical", "critic_verdict": "pass",
            "mergeable": True,
        }],
    }
    m2 = _mf.apply_terminal(m, r_b, terminal_required=True)
    assert m2["terminal"]["verdict"] == _tm.TERMINAL_CONTROL_ANOMALY
    assert m2["accepted"][0]["terminal"] == _tm.TERMINAL_CONTROL_ANOMALY
    assert m2["accepted"][0]["mergeable"] is False

    # Spec resolvers: default bound when lanes declared; empty when absent
    sp_lanes = SimpleNamespace(
        control_lanes=CONTROL, control_composition_bound_pct=None, raw={})
    assert _tm.resolve_control_lanes(sp_lanes) == CONTROL
    assert _tm.resolve_control_composition_bound_pct(sp_lanes) == 2.0
    sp_none = SimpleNamespace(control_lanes=[], raw={})
    assert _tm.resolve_control_lanes(sp_none) == []
    sp_raw = SimpleNamespace(
        control_lanes=None, control_composition_bound_pct=None,
        raw={"control_lanes": ["revm_pinned"], "control_composition_bound_pct": 1.5})
    assert _tm.resolve_control_lanes(sp_raw) == ["revm_pinned"]
    assert _tm.resolve_control_composition_bound_pct(sp_raw) == 1.5
    print("#47f OK: apply_terminal mergeable=false + control resolvers")
    print("case 36 OK")

def case_38():
    """T16: outlier quarantine — |Δ| above threshold → mergeable=false + reason.

    Default threshold is 5.0 even when the field is absent (default-on tripwire).
    Explicit 0 disables. Both build_manifest and apply_terminal must agree.
    """
    from aro import manifest as _mf
    from aro import terminal as _tm
    from aro import spec as _specmod

    def _run(d, delta_pct, *, regime="byte-identical", critic="pass"):
        d = Path(d)
        def J(o):
            return json.dumps(o)
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": regime, "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0",
             "verdict": critic},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": delta_pct,
                         "improved": delta_pct < 0}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")

    # Spec field: default 5.0 when absent; explicit 0 disables.
    bare = _specmod.from_dict({
        "name": "oq", "target_repo": {"path": "."}, "metric": "ns",
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "k"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
    })
    assert bare.outlier_quarantine_pct == 5.0
    off = _specmod.from_dict({
        "name": "oq0", "target_repo": {"path": "."}, "metric": "ns",
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "k"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "outlier_quarantine_pct": 0,
    })
    assert off.outlier_quarantine_pct == 0.0
    print("#49a OK: outlier_quarantine_pct default 5.0; explicit 0 disables")

    conf = _tm.judge_terminal(
        _tm.MeasureDoc(
            rows={"mega_bench/sload": 10000},
            meta={"profile_fingerprint": "fp-oq"},
            profile_fingerprint="fp-oq"),
        _tm.MeasureDoc(
            rows={"mega_bench/sload": 9680},
            meta={"profile_fingerprint": "fp-oq"},
            profile_fingerprint="fp-oq"),
        epsilon_pct=0.1)
    assert conf.verdict == _tm.TERMINAL_CONFIRMED

    # (1) |Δ| below threshold → untouched vs tripwire-off (byte-identical)
    with tempfile.TemporaryDirectory() as d:
        _run(d, -4.5)
        m_def = _mf.build_manifest(d)  # default 5.0
        m_off = _mf.build_manifest(d, outlier_quarantine_pct=0)
        assert m_def["accepted"][0]["mergeable"] is True
        assert "quarantine" not in m_def["accepted"][0]
        assert json.dumps(m_def, sort_keys=True) == json.dumps(m_off, sort_keys=True)
        assert _mf.status_flag(m_def["accepted"][0]) == "MERGEABLE "
    print("#49b OK: |Δ| under threshold → no quarantine, byte-identical to off")

    # (2) |Δ| above threshold + everything else CONFIRMED/pass → quarantined
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _run(d, -19.15)
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(conf.to_dict(), ensure_ascii=False, indent=1) + "\n")
        m = _mf.build_manifest(
            d, terminal_result=conf, terminal_required=True,
            terminal_source=str(tpath))
        a = m["accepted"][0]
        assert a["mergeable"] is False, a
        assert a.get("quarantine", "").startswith("outlier:"), a
        assert "|Δ|=19.150%" in a["quarantine"] and "> 5.0%" in a["quarantine"], a
        assert a["terminal"] == "TERMINAL_CONFIRMED"
        assert a["regime"] == "byte-identical" and a["critic_verdict"] == "pass"
        assert _mf.status_flag(a) == "needs-review (outlier)"
        # is_mergeable alone would still say True — quarantine is post-filter
        assert _mf.is_mergeable(
            "byte-identical", "pass",
            terminal="TERMINAL_CONFIRMED", terminal_required=True,
            terminal_stamp=a["terminal_stamp"]) is True
    print("#49c OK: outlier + CONFIRMED/pass → mergeable=false, display outlier")

    # (3) outlier_quarantine_pct: 0 → tripwire off, legacy mergeable true
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _run(d, -19.15)
        m0 = _mf.build_manifest(d, outlier_quarantine_pct=0)
        assert m0["accepted"][0]["mergeable"] is True
        assert "quarantine" not in m0["accepted"][0]
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(conf.to_dict(), ensure_ascii=False, indent=1) + "\n")
        m0t = _mf.build_manifest(
            d, terminal_result=conf, terminal_required=True,
            outlier_quarantine_pct=0, terminal_source=str(tpath))
        assert m0t["accepted"][0]["mergeable"] is True
        assert "quarantine" not in m0t["accepted"][0]
        assert m0t["accepted"][0]["terminal_stamp"]["sha256"] == \
            _mf.terminal_file_sha256(tpath)
    print("#49d OK: threshold 0 → tripwire off, legacy mergeable output")

    # (4) both paths agree on the same quarantine decision
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _run(d, -12.0)
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(conf.to_dict(), ensure_ascii=False, indent=1) + "\n")
        m_build = _mf.build_manifest(
            d, terminal_result=conf, terminal_required=True,
            outlier_quarantine_pct=5.0, terminal_source=str(tpath))
        m_base = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        # base (no terminal) is already non-mergeable due to quarantine alone
        assert m_base["accepted"][0]["mergeable"] is False
        m_apply = _mf.apply_terminal(
            m_base, conf, terminal_required=True,
            outlier_quarantine_pct=5.0, source=str(tpath))
        b, a = m_build["accepted"][0], m_apply["accepted"][0]
        assert b["mergeable"] is False and a["mergeable"] is False
        assert b.get("quarantine") == a.get("quarantine")
        assert b["quarantine"].startswith("outlier:")
        # Never auto-promote: apply_terminal with CONFIRMED must not clear
        # quarantine for an outlier even though is_mergeable would be True.
        assert a["terminal"] == "TERMINAL_CONFIRMED"
        assert a["mergeable"] is False
        assert a.get("terminal_stamp", {}).get("verdict") == "TERMINAL_CONFIRMED"
    print("#49e OK: build_manifest and apply_terminal agree on quarantine")

    # (5) positive and negative deltas both quarantine (absolute value)
    for delta in (-19.15, +19.15, -5.001, 5.001):
        with tempfile.TemporaryDirectory() as d:
            _run(d, delta)
            m = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
            a = m["accepted"][0]
            assert a["mergeable"] is False, (delta, a)
            assert "quarantine" in a, (delta, a)
            assert _mf.status_flag(a) == "needs-review (outlier)"
    # Exactly at threshold is NOT quarantined (strict >)
    with tempfile.TemporaryDirectory() as d:
        _run(d, -5.0)
        m = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        assert m["accepted"][0]["mergeable"] is True
        assert "quarantine" not in m["accepted"][0]
    print("#49f OK: |Δ| both signs quarantine; exact threshold not")
    print("case 38 OK")

def case_40():
    """T19: verdict integrity — recompute on load, tool-only manifest stamps.

    Hermetic: verify_terminal_doc, rejudge rejects tampered input, apply_terminal
    writes terminal_stamp, mergeable requires stamp+CONFIRMED, CLI hash check.
    Existing judge_terminal cases (case_31 #42c) stay green unmodified.
    """
    print("=== case 40: terminal verdict integrity ===")
    import hashlib
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from aro import terminal as _tm
    from aro import manifest as _mf

    def _doc(rows, fp="fp-int"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    # (a) consistent doc → verify passes; verdict_from_rows matches judge
    r_ok = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 20000}),
        epsilon_pct=0.1)
    assert r_ok.verdict == _tm.TERMINAL_CONFIRMED
    doc_ok = r_ok.to_dict()
    _tm.verify_terminal_doc(doc_ok)  # must not raise
    v, imp, reg, ce = _tm.verdict_from_rows(doc_ok["rows"])
    assert v == _tm.TERMINAL_CONFIRMED and imp == 1 and reg == 0 and ce == 0
    print("#51a OK: consistent doc verifies; verdict_from_rows matches")

    # (b) tampered verdict field (MIXED→CONFIRMED) → TerminalError naming verdict
    r_mix = _tm.judge_terminal(
        _doc({"a": 10000, "b": 20000}),
        _doc({"a": 9000, "b": 22000}),
        epsilon_pct=0.1)
    assert r_mix.verdict == _tm.TERMINAL_MIXED
    doc_v = r_mix.to_dict()
    doc_v["verdict"] = _tm.TERMINAL_CONFIRMED
    try:
        _tm.verify_terminal_doc(doc_v)
        assert False, "tampered verdict must hard-error"
    except _tm.TerminalError as e:
        assert "verdict" in str(e).lower(), e
        assert "TERMINAL_CONFIRMED" in str(e) and "TERMINAL_MIXED" in str(e)
    print("#51b OK: tampered verdict → TerminalError names verdict")

    # (c) tampered row: cand_ir lowered but delta/status stale
    doc_r = r_mix.to_dict()
    # Lower cand_ir on the regressed row so recomputed Δ is improved-ish, but
    # leave stored delta_pct / status as the old regressed values.
    for row in doc_r["rows"]:
        if row["row_key"] == "b":
            row["cand_ir"] = 18000  # was 22000; real Δ = -10%, stored still +10%
            break
    try:
        _tm.verify_terminal_doc(doc_r)
        assert False, "stale delta after cand_ir edit must hard-error"
    except _tm.TerminalError as e:
        assert "b" in str(e) and "delta_pct" in str(e), e

    # delta edited but verdict stale (rows still say MIXED, verdict CONFIRMED already covered;
    # edit only delta on improved row to mismatch without fixing status)
    doc_d = r_mix.to_dict()
    for row in doc_d["rows"]:
        if row["row_key"] == "a":
            row["delta_pct"] = -50.0  # stored lie; recomputed is -10%
            break
    try:
        _tm.verify_terminal_doc(doc_d)
        assert False, "stale delta_pct must hard-error"
    except _tm.TerminalError as e:
        assert "a" in str(e) and "delta_pct" in str(e), e
    print("#51c OK: tampered row cand_ir/delta → TerminalError names row")

    # (d) rejudge on tampered input → errors before writing any output file
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        bad = r_mix.to_dict()
        bad["verdict"] = _tm.TERMINAL_CONFIRMED
        in_path = d / "terminal.json"
        in_path.write_text(json.dumps(bad, ensure_ascii=False, indent=1) + "\n")
        out_path = Path(str(in_path) + ".rejudged.json")
        try:
            _tm.rejudge_terminal_doc(
                json.loads(in_path.read_text()),
                epsilon_pct=0.1, floors={}, default_floor_pct=0.1)
            assert False, "rejudge must reject tampered input"
        except _tm.TerminalError as e:
            assert "verdict" in str(e).lower(), e
        assert not out_path.exists(), "rejudge must not write output on tamper"

        # CLI path: SystemExit(2), no .rejudged.json
        spec_path = d / "spec.json"
        spec_path.write_text(json.dumps({
            "name": "int-smoke",
            "target_repo": {"path": str(d / "no-repo")},
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "metric": "ns_per_call",
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "terminal_bench_targets": ["mega_bench"],
            "icount_epsilon_pct": 0.1,
        }))
        try:
            _tm.cli(SimpleNamespace(
                spec=str(spec_path), rejudge=str(in_path),
                list=False, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=None, hypothesis=None, events_ref=None))
            assert False, "CLI rejudge must SystemExit on tamper"
        except SystemExit as se:
            assert se.code == 2, se
        assert not out_path.exists()
    print("#51d OK: rejudge rejects tampered input, no output file")

    # (e) apply_terminal writes stamp; mergeable requires stamp+CONFIRMED;
    #     legacy bare terminal without stamp → not mergeable + unstamped listing
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def J(o):
            return json.dumps(o)

        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")

        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(r_ok.to_dict(), ensure_ascii=False, indent=1) + "\n")
        expected_sha = hashlib.sha256(tpath.read_bytes()).hexdigest()

        m = _mf.build_manifest(d)
        m = _mf.apply_terminal(
            m, r_ok, terminal_required=True, source=str(tpath),
            outlier_quarantine_pct=0)
        a = m["accepted"][0]
        assert a["mergeable"] is True, a
        assert a["terminal"] == _tm.TERMINAL_CONFIRMED
        st = a["terminal_stamp"]
        assert st["verdict"] == _tm.TERMINAL_CONFIRMED
        assert st["source"] == str(tpath)
        assert st["sha256"] == expected_sha
        assert m["terminal"]["terminal_stamp"]["sha256"] == expected_sha

        # Legacy bare terminal WITHOUT stamp → not mergeable + unstamped flag
        m_legacy = _mf.build_manifest(d, terminal_required=True)
        m_legacy["accepted"][0]["terminal"] = _tm.TERMINAL_CONFIRMED
        m_legacy["accepted"][0]["mergeable"] = _mf.is_mergeable(
            "byte-identical", "pass",
            terminal=_tm.TERMINAL_CONFIRMED, terminal_required=True)
        assert m_legacy["accepted"][0]["mergeable"] is False
        assert "terminal_stamp" not in m_legacy["accepted"][0]
        assert _mf.status_flag(m_legacy["accepted"][0]) == \
            "needs-review (unstamped terminal)"
    print("#51e OK: apply_terminal stamp + unstamped legacy not mergeable")

    # (f) stamp hash mismatch after file modification → hard error in CLI path
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def J(o):
            return json.dumps(o)

        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(r_ok.to_dict(), ensure_ascii=False, indent=1) + "\n")
        m = _mf.build_manifest(
            d, terminal_result=r_ok, terminal_required=True,
            terminal_source=str(tpath), outlier_quarantine_pct=0)
        # Mutate the source file bytes after stamp was taken
        tpath.write_text(
            json.dumps(r_mix.to_dict(), ensure_ascii=False, indent=1) + "\n")
        try:
            _mf.verify_manifest_terminal_stamps(m)
            assert False, "hash mismatch must hard-error"
        except SystemExit as se:
            assert "hash mismatch" in str(se), se
    print("#51f OK: stamp hash mismatch → hard error")

    # (g) TEST_FAILED empty-rows doc still verifies
    r_tf = _tm.TerminalResult(
        verdict=_tm.TERMINAL_TEST_FAILED, bench_ir_rows={},
        profile_fingerprint="", rows=[], notes=["fail"], rounds=0,
        floors_source="n/a")
    _tm.verify_terminal_doc(r_tf.to_dict())
    print("#51g OK: TERMINAL_TEST_FAILED empty doc verifies")

    # --- delta: control-laundering channel (lane-aware verify) ---------------
    CONTROL = ["revm_pinned", "revm_latest", "op_revm_pinned", "op_revm_latest"]
    BOUND = 2.0

    # (h) Laundering attack: relabel regressed subjects as control-ok with
    # raised floors + CONFIRMED. Lane-less self-consistency passes; lane-aware
    # verify names the first laundered row. Documents why lane-less must never
    # unlock mergeable.
    r_mix_h = _tm.judge_terminal(
        _doc({"subj/a": 10000, "subj/b": 20000}),
        _doc({"subj/a": 9000, "subj/b": 22000}),  # -10% / +10% → MIXED
        epsilon_pct=0.1)
    assert r_mix_h.verdict == _tm.TERMINAL_MIXED
    doc_launder = r_mix_h.to_dict()
    laundered_key = None
    for row in doc_launder["rows"]:
        if row["status"] == "regressed":
            laundered_key = row["row_key"]
            row["status"] = "control-ok"
            row["floor_pct"] = abs(float(row["delta_pct"])) + 1.0
            break
    assert laundered_key is not None
    doc_launder["verdict"] = _tm.TERMINAL_CONFIRMED
    # Lane-less: self-consistent (control class from stored prefix + raised floor)
    _tm.verify_terminal_doc(doc_launder)
    try:
        _tm.verify_terminal_doc(
            doc_launder, control_lanes=CONTROL, control_bound_pct=BOUND)
        assert False, "laundered control-ok subject must fail lane-aware verify"
    except _tm.TerminalError as e:
        assert laundered_key in str(e), e
        assert "control-class" in str(e), e
    # Empty lanes: any control-* status is itself an error
    try:
        _tm.verify_terminal_doc(doc_launder, control_lanes=[])
        assert False, "control-* with control_lanes=[] must error"
    except _tm.TerminalError as e:
        assert laundered_key in str(e), e
    # Lane-less stamp path would still hash the laundered file — production
    # mergeable paths must pass control_lanes so this cannot unlock mergeable.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tpath = d / "terminal.json"
        tpath.write_text(
            json.dumps(doc_launder, ensure_ascii=False, indent=1) + "\n")
        # Lane-less stamp succeeds (self-consistent) — must NOT be the
        # mergeable-unlocking path.
        st_less = _mf.build_terminal_stamp_from_source(tpath)
        assert st_less["verdict"] == _tm.TERMINAL_CONFIRMED
        # Spec-verified (lane-aware) stamp path rejects the laundered doc.
        try:
            _mf.build_terminal_stamp_from_source(
                tpath, control_lanes=CONTROL, control_bound_pct=BOUND)
            assert False, "lane-aware stamp must reject laundered doc"
        except _tm.TerminalError as e:
            assert laundered_key in str(e), e
        try:
            _mf.build_terminal_stamp_from_source(tpath, control_lanes=[])
            assert False, "empty-lanes stamp must reject control-* status"
        except _tm.TerminalError as e:
            assert laundered_key in str(e), e
    print("#51h OK: laundering caught by lane-aware verify; lane-less still passes")

    # (i) Genuine control rows verify with matching bound; wrong floor → error
    r_ctrl = _tm.judge_terminal(
        _doc({
            "log_opcodes/rex4/log4_32b": 10000,
            "log_opcodes/op_revm_latest/log4_32b": 20000,
        }),
        _doc({
            "log_opcodes/rex4/log4_32b": 9000,         # subject improved
            "log_opcodes/op_revm_latest/log4_32b": 20100,  # +0.5% control-ok
        }),
        epsilon_pct=0.1,
        control_lanes=CONTROL,
        control_composition_bound_pct=BOUND,
    )
    assert r_ctrl.verdict == _tm.TERMINAL_CONFIRMED
    doc_ctrl = r_ctrl.to_dict()
    _tm.verify_terminal_doc(
        doc_ctrl, control_lanes=CONTROL, control_bound_pct=BOUND)
    # Inflate control floor above bound → floor mismatch (even if status still ok)
    doc_bad_floor = json.loads(json.dumps(doc_ctrl))
    ctrl_key = None
    for row in doc_bad_floor["rows"]:
        if str(row.get("status", "")).startswith("control-"):
            ctrl_key = row["row_key"]
            row["floor_pct"] = BOUND + 3.0
            break
    assert ctrl_key is not None
    try:
        _tm.verify_terminal_doc(
            doc_bad_floor, control_lanes=CONTROL, control_bound_pct=BOUND)
        assert False, "control floor ≠ bound must error"
    except _tm.TerminalError as e:
        assert ctrl_key in str(e) and "floor" in str(e).lower(), e
    print("#51i OK: genuine control verifies; floor≠bound errors")

    # (j) Spec without control_lanes (pass []): any stored control-* → error
    try:
        _tm.verify_terminal_doc(doc_ctrl, control_lanes=[])
        assert False, "control-* with empty lanes must error"
    except _tm.TerminalError as e:
        assert "control-class" in str(e), e
    print("#51j OK: control_lanes=[] rejects any control-* status")

    # (k) --rejudge and --update-manifest exercise lane-aware path (no subprocess)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # Laundered doc on disk
        bad_path = d / "terminal.json"
        bad_path.write_text(
            json.dumps(doc_launder, ensure_ascii=False, indent=1) + "\n")
        # Spec declares control_lanes so CLI rejudge is lane-aware
        spec_path = d / "spec.json"
        spec_path.write_text(json.dumps({
            "name": "int-lane",
            "target_repo": {"path": str(d / "no-repo")},
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "metric": "ns_per_call",
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "terminal_bench_targets": ["mega_bench"],
            "icount_epsilon_pct": 0.1,
            "control_lanes": CONTROL,
            "control_composition_bound_pct": BOUND,
        }))
        out_re = Path(str(bad_path) + ".rejudged.json")
        try:
            _tm.cli(SimpleNamespace(
                spec=str(spec_path), rejudge=str(bad_path),
                list=False, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=None, hypothesis=None, events_ref=None))
            assert False, "CLI rejudge must SystemExit on laundered input"
        except SystemExit as se:
            assert se.code == 2, se
        assert not out_re.exists()

        # --update-manifest stamp path: spy verify kwargs via apply_terminal
        # (same call site as cli update-manifest after measure).
        seen = {}
        real_verify = _tm.verify_terminal_doc

        def _spy(doc, *, control_lanes=None, control_bound_pct=None):
            seen["control_lanes"] = control_lanes
            seen["control_bound_pct"] = control_bound_pct
            return real_verify(
                doc, control_lanes=control_lanes,
                control_bound_pct=control_bound_pct)

        # Clean subject-only CONFIRMED file for a successful lane-aware stamp
        clean = r_ok.to_dict()
        clean_path = d / "clean-terminal.json"
        clean_path.write_text(
            json.dumps(clean, ensure_ascii=False, indent=1) + "\n")
        # Build a minimal manifest dir
        def J(o):
            return json.dumps(o)
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        m = _mf.build_manifest(d)
        _tm.verify_terminal_doc = _spy  # type: ignore[method-assign]
        try:
            # Production update-manifest always passes lanes from the spec.
            m2 = _mf.apply_terminal(
                m, r_ok, terminal_required=True, source=str(clean_path),
                outlier_quarantine_pct=0,
                control_lanes=CONTROL, control_bound_pct=BOUND)
            assert seen.get("control_lanes") == CONTROL, seen
            assert seen.get("control_bound_pct") == BOUND, seen
            assert m2["accepted"][0]["mergeable"] is True
            # Laundered source rejected on the same path
            try:
                _mf.apply_terminal(
                    dict(m), r_ok, terminal_required=True, source=str(bad_path),
                    outlier_quarantine_pct=0,
                    control_lanes=CONTROL, control_bound_pct=BOUND)
                assert False, "update-manifest path must reject laundered source"
            except _tm.TerminalError as e:
                assert laundered_key in str(e), e
        finally:
            _tm.verify_terminal_doc = real_verify  # type: ignore[method-assign]
    print("#51k OK: rejudge + update-manifest exercise lane-aware verify")
    print("case 40 OK")

