"""T48: probe-lane terminal — certification without a criterion/CodSpeed suite.

Hermetic: fake probe/factory runners only. No cargo, no network, no measure_bin.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


def case_64():
    """T48: terminal_lane probe vs bench — matrix, calibrate, stamp, package disclosure."""
    from aro import terminal as tm
    from aro import manifest as mf
    from aro import ship as shipmod
    from aro import spec as specmod

    print("=== case 64: probe-lane terminal (T48) ===")

    # --- helpers ---------------------------------------------------------------
    def _base_spec_dict(*, name="probe-lane-t48", terminal_lane=None,
                        terminal_probe_workloads=None,
                        terminal_bench_targets=None,
                        bench_scales=None, control_lanes=None):
        d = {
            "name": name,
            "target_repo": {"path": "/tmp/does-not-need-to-exist-t48",
                            "baseline_ref": "HEAD"},
            "metric": "ns_per_call",
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "run": {
                "aa_runs": 1, "ab_pairs": 1, "timeout": 60,
                "bench_scales": bench_scales if bench_scales is not None
                else [1, 8],
            },
            "icount_epsilon_pct": 0.1,
            "terminal_default_floor_pct": 1.0,
            "terminal_measure_rounds": 1,
        }
        if terminal_lane is not None:
            d["terminal_lane"] = terminal_lane
        if terminal_probe_workloads is not None:
            d["terminal_probe_workloads"] = terminal_probe_workloads
        if terminal_bench_targets is not None:
            d["terminal_bench_targets"] = terminal_bench_targets
        if control_lanes is not None:
            d["control_lanes"] = control_lanes
        return d

    def _sp(**kw):
        return specmod.from_dict(_base_spec_dict(**kw))

    def _fake_factory_2(spec, baseline=None):
        """Yield exactly two variants (test-injected factory)."""
        return [
            tm.ProbeVariant(
                name="orig",
                params={"kind": "original", "probe": "p.rs"}),
            tm.ProbeVariant(
                name="vA",
                params={"kind": "synthetic", "seed": 42, "probe": "p.rs"}),
        ]

    def _icount_for_side(side_table):
        """side_table: {(variant, scale): ir} or callable(checkout)->table."""
        def _run(checkout, variant, scale):
            key = (variant.name, int(scale))
            if callable(side_table):
                table = side_table(checkout)
            else:
                table = side_table
            # Tag checkout path so base vs cand can differ.
            co = str(checkout)
            if co in table:
                return table[co][key]
            if key in table:
                return table[key]
            raise AssertionError(f"no Ir for {key} checkout={co!r}")
        return _run

    # --- (a) spec: absent → bench; explicit values; invalid → SystemExit ------
    sp_absent = _sp()
    assert sp_absent.terminal_lane == "bench", sp_absent.terminal_lane
    assert tm.resolve_terminal_lane(sp_absent) == tm.TERMINAL_LANE_BENCH
    assert sp_absent.terminal_probe_workloads == 4
    assert tm.has_terminal_config(sp_absent) is False  # no bench targets

    sp_probe = _sp(terminal_lane="probe", terminal_probe_workloads=2)
    assert sp_probe.terminal_lane == "probe"
    assert sp_probe.terminal_probe_workloads == 2
    assert tm.has_terminal_config(sp_probe) is True  # probe lane alone enables gate

    sp_bench = _sp(terminal_lane="bench", terminal_bench_targets=["mega_bench"])
    assert sp_bench.terminal_lane == "bench"
    assert tm.has_terminal_config(sp_bench) is True

    for bad in ("Probe", "criterion", "", " "):
        try:
            _sp(terminal_lane=bad if bad.strip() else bad)
            # empty/whitespace falls through to default when strip empties —
            # only non-empty invalid tokens SystemExit.
            if not str(bad).strip():
                continue
            assert False, f"invalid terminal_lane {bad!r} must SystemExit"
        except SystemExit as e:
            assert "terminal_lane" in str(e), e

    try:
        _sp(terminal_lane="not-a-lane")
        assert False, "invalid terminal_lane must SystemExit"
    except SystemExit as e:
        assert "terminal_lane" in str(e)
    print("#64a OK: terminal_lane absent→bench; explicit; invalid SystemExit")

    # --- (b) bench mode: empty targets hard error (message pinned) -----------
    sp_empty_bench = _sp(terminal_lane="bench")  # no terminal_bench_targets
    assert tm.has_terminal_config(sp_empty_bench) is False
    try:
        tm.run_terminal(
            sp_empty_bench, "/tmp/b", "/tmp/c", skip_selfcheck=True)
        assert False, "bench lane without targets must hard-error"
    except tm.TerminalError as e:
        msg = str(e)
        assert "spec has no terminal_bench_targets — terminal gate not configured" in msg, msg

    # measure_checkout empty targets (direct) pins the other message
    try:
        tm.measure_checkout(
            "/tmp/x", package="p", bench_targets=[],
            measure_bin="/fake/r",
            runner=lambda cmd, timeout=None: ("{}", "", 0))
        assert False, "empty bench_targets must hard-error"
    except tm.TerminalError as e:
        assert str(e) == "terminal_bench_targets is empty — nothing to measure", e

    # calibrate same gate
    try:
        tm.run_calibrate(sp_empty_bench, "/tmp/x", rounds=2, skip_selfcheck=True)
        assert False, "bench calibrate without targets must hard-error"
    except tm.TerminalError as e:
        assert "spec has no terminal_bench_targets — nothing to calibrate" in str(e)
    print("#64b OK: bench empty targets hard error (messages pinned)")

    # --- (c) probe mode: row matrix + calibrate floors + CONFIRMED / MIXED ----
    scales = [1, 8]
    variants = _fake_factory_2(None)
    expected_keys = tm.probe_row_keys(variants, scales)
    assert expected_keys == [
        "probe/orig/1", "probe/orig/8",
        "probe/vA/1", "probe/vA/8",
    ], expected_keys

    # Stable floor tables: base and cand tables keyed by checkout path.
    base_irs = {
        ("orig", 1): 10000, ("orig", 8): 80000,
        ("vA", 1): 11000, ("vA", 8): 88000,
    }
    # CONFIRMED: every subject row improves beyond floor (or stays equal within floor)
    cand_confirmed = {
        ("orig", 1): 9000, ("orig", 8): 72000,   # -10%
        ("vA", 1): 9900, ("vA", 8): 79200,       # -10%
    }
    # MIXED: one improves, one regresses
    cand_mixed = {
        ("orig", 1): 9000, ("orig", 8): 72000,   # improved
        ("vA", 1): 12100, ("vA", 8): 96800,      # +10% regressed
    }

    def _runner_pair(cand_table):
        def _side(checkout):
            co = str(checkout)
            if "cand" in co:
                return cand_table
            return base_irs
        return _icount_for_side(_side)

    floors = {k: 0.5 for k in expected_keys}
    sp_p = _sp(terminal_lane="probe", terminal_probe_workloads=1,
               bench_scales=scales)

    r_ok = tm.run_terminal(
        sp_p, "/tmp/base-wt", "/tmp/cand-wt",
        rounds=1, floors=floors, skip_selfcheck=True,
        probe_factory=_fake_factory_2,
        probe_icount_runner=_runner_pair(cand_confirmed))
    assert r_ok.verdict == tm.TERMINAL_CONFIRMED, r_ok
    assert r_ok.terminal_lane == "probe"
    assert r_ok.control_lanes == []
    assert sorted(rd.row_key for rd in r_ok.rows) == sorted(expected_keys)
    doc_ok = tm.terminal_doc_dict(r_ok, baseline_sha="a" * 40)
    assert doc_ok["terminal_lane"] == "probe"
    assert doc_ok["control_lanes"] == []
    assert len(doc_ok["probe_variants"]) == 2
    names = {v["name"] for v in doc_ok["probe_variants"]}
    assert names == {"orig", "vA"}, names
    # Identities recorded (params present)
    for v in doc_ok["probe_variants"]:
        assert "params" in v and v["params"], v

    r_mix = tm.run_terminal(
        sp_p, "/tmp/base-wt", "/tmp/cand-wt",
        rounds=1, floors=floors, skip_selfcheck=True,
        probe_factory=_fake_factory_2,
        probe_icount_runner=_runner_pair(cand_mixed))
    assert r_mix.verdict == tm.TERMINAL_MIXED, r_mix
    assert r_mix.terminal_lane == "probe"
    print("#64c OK: probe matrix + CONFIRMED/MIXED + variant identities + control_lanes=[]")

    # --- (d) calibrate writes floors for exactly those rows -------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["ARO_FLOORS_DIR"] = str(td)
        try:
            # A/A: tiny jitter so floors are positive but finite
            call_n = {"n": 0}

            def _aa_runner(checkout, variant, scale):
                call_n["n"] += 1
                base = base_irs[(variant.name, int(scale))]
                # alternate +0 / +1 Ir for pairwise noise
                return base + (call_n["n"] % 2)

            payload = tm.run_calibrate(
                sp_p, "/tmp/checkout-wt", rounds=3, skip_selfcheck=True,
                probe_factory=_fake_factory_2,
                probe_icount_runner=_aa_runner,
                out_path=td / "probe-lane-t48.json")
            assert set(payload["floors"].keys()) == set(expected_keys), payload["floors"]
            assert payload["meta"]["terminal_lane"] == "probe"
            assert Path(payload["path"]).is_file()
            for k, fl in payload["floors"].items():
                assert fl >= 0.1, (k, fl)  # ε clamp
        finally:
            del os.environ["ARO_FLOORS_DIR"]
    print("#64d OK: probe calibrate floors for exact matrix rows")

    # --- (e) stamp carries terminal_lane; rejudge preserves it ---------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        term_path = td / "terminal.json"
        doc = tm.terminal_doc_dict(r_ok, baseline_sha="b" * 40)
        assert doc["terminal_lane"] == "probe"
        term_path.write_text(json.dumps(doc, indent=1) + "\n")
        stamp = mf.build_terminal_stamp_from_source(
            term_path, control_lanes=[])
        assert stamp["terminal_lane"] == "probe", stamp
        assert stamp["verdict"] == tm.TERMINAL_CONFIRMED
        assert stamp["baseline_sha"] == "b" * 40

        # rejudge preserves terminal_lane + probe_variants
        rej = tm.rejudge_terminal_doc(
            doc, epsilon_pct=0.1, floors=floors, default_floor_pct=1.0,
            control_lanes=[])
        assert rej.terminal_lane == "probe"
        assert rej.control_lanes == []
        assert rej.probe_variants  # preserved
        rej_doc = tm.terminal_doc_dict(rej, baseline_sha=doc["baseline_sha"])
        assert rej_doc["terminal_lane"] == "probe"
        assert rej_doc["control_lanes"] == []
        assert len(rej_doc.get("probe_variants") or []) == 2
    print("#64e OK: stamp terminal_lane=probe; rejudge preserves it")

    # --- (f) package body disclosure present for probe, absent for bench ------
    def _body_for_lane(lane: str) -> str:
        with tempfile.TemporaryDirectory() as tdx:
            tdx = Path(tdx)
            run = tdx / "run"
            run.mkdir()
            term = {
                "verdict": "TERMINAL_CONFIRMED",
                "bench_ir_rows": {"probe/orig/1": -5.0},
                "profile_fingerprint": "fp",
                "epsilon_pct": 0.1,
                "rounds": 1,
                "floors_source": "default",
                "baseline_sha": "c" * 40,
                "terminal_lane": lane,
                "control_lanes": [] if lane == "probe" else None,
                "notes": [],
                "rows": [
                    {"row_key": "probe/orig/1", "base_ir": 1000, "cand_ir": 950,
                     "delta_pct": -5.0, "status": "improved", "floor_pct": 0.5},
                ],
            }
            if lane != "probe":
                term.pop("control_lanes", None)
            tpath = run / "terminal.json"
            tpath.write_text(json.dumps(term, indent=1) + "\n")
            sha = __import__("hashlib").sha256(tpath.read_bytes()).hexdigest()
            stamp = {
                "verdict": "TERMINAL_CONFIRMED",
                "source": str(tpath),
                "sha256": sha,
                "baseline_sha": "c" * 40,
            }
            if lane == "probe":
                stamp["terminal_lane"] = "probe"
            man = {
                "baseline_ref": "c" * 40,
                "accepted": [{
                    "order": 1, "id": "c1", "fn": "hot",
                    "mergeable": True, "regime": "byte-identical",
                    "critic_verdict": "pass", "delta_pct": -5.0,
                    "patch_path": "a1/patches/c1.txt",
                    "files": ["src/lib.rs"],
                    "terminal": "TERMINAL_CONFIRMED",
                    "terminal_stamp": stamp,
                    "bench_ir_rows": {"probe/orig/1": -5.0},
                }],
            }
            # Minimal repo-shaped spec for body gen (no real package apply).
            sp = _sp(terminal_lane=lane if lane == "probe" else "bench",
                     name="body-lane")
            return shipmod.generate_pr_body(
                sp, man, run_dir=run, run_name="run",
                mergeable=man["accepted"], files_changed=["src/lib.rs"])

    body_probe = _body_for_lane("probe")
    assert tm.PROBE_LANE_DISCLOSURE in body_probe, body_probe
    assert "probe-lane (no independent bench suite)" in body_probe
    body_bench = _body_for_lane("bench")
    assert tm.PROBE_LANE_DISCLOSURE not in body_bench, body_bench
    assert "probe-lane (no independent bench suite)" not in body_bench
    print("#64f OK: package body probe disclosure present; bench absent")

    # --- (g) default factory: original + K synthetic, deterministic -----------
    sp_k = _sp(terminal_lane="probe", terminal_probe_workloads=3,
               bench_scales=[1])
    vs = tm.default_probe_variants(sp_k, baseline="deadbeef")
    assert vs[0].name == "original"
    assert len(vs) == 1 + 3  # original + K
    assert [v.name for v in vs[1:]] == ["v1", "v2", "v3"]
    vs2 = tm.default_probe_variants(sp_k, baseline="deadbeef")
    assert [v.params.get("seed") for v in vs[1:]] == [
        v.params.get("seed") for v in vs2[1:]]
    # different baseline → different seeds
    vs3 = tm.default_probe_variants(sp_k, baseline="cafebabe")
    assert [v.params.get("seed") for v in vs[1:]] != [
        v.params.get("seed") for v in vs3[1:]]
    print("#64g OK: default factory original+K deterministic per (spec, baseline)")

    # --- (h) probe CLI --list shows lane --------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spath = td / "probe.json"
        spath.write_text(json.dumps(_base_spec_dict(
            terminal_lane="probe", terminal_probe_workloads=2,
            bench_scales=[1, 8])))
        buf = io.StringIO()
        with redirect_stdout(buf):
            tm.cli(SimpleNamespace(
                spec=str(spath), list=True, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=None, hypothesis=None, events_ref=None,
                rejudge=None, orders=None, calibrate=False))
        out = buf.getvalue()
        assert "terminal_lane:          probe" in out, out
        assert "terminal_probe_scales:" in out, out
        assert "gate active:            True" in out
        assert "control_lanes:          [] (vacuous under probe lane)" in out
    print("#64h OK: CLI --list probe lane")

    print("case_64 OK: probe-lane terminal (T48)")
