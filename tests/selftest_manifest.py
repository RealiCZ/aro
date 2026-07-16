from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace


def case_21():
    # --- #27: manifest reconstruction (the hand-off artifact) ----------------
    # An OLD-format run (no `attempt` stamp) with the id collision that breaks naive
    # consumers: agent-r0-0 is BOTH a relaxed/pass-risk win (a1) and a byte-identical/
    # pass win (a2). The manifest must resolve each to its own attempt dir + patch, and
    # mark only the clean byte-identical one mergeable.
    from aro import manifest as manifestmod
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        def J(o): return json.dumps(o)
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sstore",
             "regime": "relaxed", "files": ["crates/x/src/a.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass-risk"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -19.2, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "cache"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0", "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text("\n".join(J(e) for e in evs) + "\n")
        for a, repl in (("a1", "crates/x/src/a.rs"), ("a2", "crates/x/src/b.rs")):
            pd = d / a / "patches"; pd.mkdir(parents=True)
            (pd / "agent-r0-0.txt").write_text(
                f"--- edit 1 ---\npath: {repl}\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        m = manifestmod.build_manifest(d)
        assert m["baseline_ref"] == "abc123" and m["spec"] == "demo", m
        acc = m["accepted"]
        assert [a["attempt"] for a in acc] == ["a1", "a2"], acc        # collision resolved by attempt
        assert acc[0]["fn"] == "sstore" and acc[0]["files"] == ["crates/x/src/a.rs"]
        assert acc[1]["fn"] == "sload" and acc[1]["files"] == ["crates/x/src/b.rs"]
        assert acc[0]["delta_pct"] == -19.2 and acc[1]["delta_pct"] == -4.5
        assert acc[0]["mergeable"] is False                            # relaxed/pass-risk
        assert acc[1]["mergeable"] is True                             # byte-identical/pass
        assert acc[0]["patch_path"] == "a1/patches/agent-r0-0.txt"
        assert m["files_touched"] == ["crates/x/src/a.rs", "crates/x/src/b.rs"], m
        # T24: explicit acceptance chain from event-stream indices + parent links
        assert acc[0]["acceptance_seq"] < acc[1]["acceptance_seq"], acc
        assert acc[0]["parent"] == "abc123"  # first parent = baseline_ref
        assert acc[1]["parent"] == "agent-r0-0"  # links to prior accepted id
        # seq points at the baseline_advanced events (indices 5 and 10 in this stream)
        assert acc[0]["acceptance_seq"] == 5 and acc[1]["acceptance_seq"] == 10, acc
    print("#27 OK: manifest resolves id-collision by attempt + flags only clean byte-identical mergeable")

    # --- T24: validate_acceptance_chain --------------------------------------
    _v = manifestmod.validate_acceptance_chain
    # consistent chain passes
    _v([
        {"order": 1, "id": "c1", "acceptance_seq": 3, "parent": "BASE"},
        {"order": 2, "id": "c2", "acceptance_seq": 7, "parent": "c1"},
        {"order": 3, "id": "c3", "acceptance_seq": 12, "parent": "c2"},
    ])
    # old entries without fields → no-op
    _v([{"order": 1, "id": "c1"}, {"order": 2, "id": "c2"}])
    _v([])
    # mixed: present fields still checked among themselves
    _v([
        {"order": 1, "id": "c1"},  # legacy, skipped
        {"order": 2, "id": "c2", "acceptance_seq": 5, "parent": "BASE"},
        {"order": 3, "id": "c3", "acceptance_seq": 9, "parent": "c2"},
    ])
    # swapped parents → error naming the entry
    try:
        _v([
            {"order": 1, "id": "c1", "acceptance_seq": 1, "parent": "BASE"},
            {"order": 2, "id": "c2", "acceptance_seq": 2, "parent": "WRONG"},
        ])
        raise AssertionError("expected ValueError for swapped parent")
    except ValueError as err:
        msg = str(err)
        assert "order=2" in msg and "c2" in msg, msg
        assert "parent" in msg.lower() or "WRONG" in msg, msg
    # non-monotonic acceptance_seq → error naming the entry
    try:
        _v([
            {"order": 1, "id": "c1", "acceptance_seq": 5, "parent": "BASE"},
            {"order": 2, "id": "c2", "acceptance_seq": 5, "parent": "c1"},
        ])
        raise AssertionError("expected ValueError for non-monotonic seq")
    except ValueError as err:
        msg = str(err)
        assert "order=2" in msg and "c2" in msg, msg
        assert "acceptance_seq" in msg, msg
    try:
        _v([
            {"order": 1, "id": "c1", "acceptance_seq": 8, "parent": "BASE"},
            {"order": 2, "id": "c2", "acceptance_seq": 3, "parent": "c1"},
        ])
        raise AssertionError("expected ValueError for decreasing seq")
    except ValueError as err:
        assert "order=2" in str(err), str(err)
    print("#T24 OK: acceptance chain fields + validate_acceptance_chain")

    # --- T22: resolve_mergeability choke point (silent; no new OK lines) -------
    # (a) single-reason status_flag strings unchanged
    # (b) unstamped + outlier surfaces BOTH reasons
    # (c) mergeable=True ⟺ empty reasons
    # (d) quarantine field serializes identically to pre-refactor path
    _mf = manifestmod
    stamp_ok = {"verdict": "TERMINAL_CONFIRMED", "source": "/t.json", "sha256": "abc"}
    # (a) single-reason labels
    assert _mf.status_flag({"mergeable": True}) == "MERGEABLE "
    assert _mf.status_flag({
        "mergeable": False,
        "quarantine": "outlier: |Δ|=19.150% > 5.0%",
    }) == "needs-review (outlier)"
    assert _mf.status_flag({
        "mergeable": False,
        "terminal": "TERMINAL_CONFIRMED",
    }) == "needs-review (unstamped terminal)"
    assert _mf.status_flag({"mergeable": False}) == "needs-review"
    # (b) multi-reason capability
    multi = {
        "mergeable": False,
        "quarantine": "outlier: |Δ|=19.150% > 5.0%",
        "terminal": "TERMINAL_CONFIRMED",
    }
    assert _mf.status_flag(multi) == "needs-review (outlier) (unstamped terminal)"
    # (c) mergeable=True ⟺ empty reasons; reason strings for each gate
    for sample in (
        _mf.resolve_mergeability(
            {"delta_pct": -1.0}, regime="byte-identical", critic_verdict="pass",
            terminal_required=False, outlier_threshold_pct=5.0),
        _mf.resolve_mergeability(
            {"delta_pct": -1.0}, regime="relaxed", critic_verdict="pass",
            terminal_required=False, outlier_threshold_pct=5.0),
        _mf.resolve_mergeability(
            {"delta_pct": -1.0}, regime="byte-identical", critic_verdict="pass-risk",
            terminal_required=False),
        _mf.resolve_mergeability(
            {"delta_pct": -1.0}, regime="byte-identical", critic_verdict="pass",
            terminal_required=True, terminal="TERMINAL_CONFIRMED"),
        _mf.resolve_mergeability(
            {"delta_pct": -1.0}, regime="byte-identical", critic_verdict="pass",
            terminal_required=True,
            terminal_stamp={"verdict": "TERMINAL_UNTOUCHED",
                            "source": "x", "sha256": "y"}),
        _mf.resolve_mergeability(
            {"delta_pct": -19.15}, regime="byte-identical", critic_verdict="pass",
            terminal_required=True, terminal="TERMINAL_CONFIRMED",
            outlier_threshold_pct=5.0),
    ):
        assert sample.mergeable is (sample.reasons == [])
    ok = _mf.resolve_mergeability(
        {"delta_pct": -1.0},
        regime="byte-identical", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=5.0)
    assert ok.mergeable is True and ok.reasons == []
    bad_regime = _mf.resolve_mergeability(
        {"delta_pct": -1.0},
        regime="relaxed", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=5.0)
    assert "regime not byte-identical" in bad_regime.reasons
    bad_critic = _mf.resolve_mergeability(
        {"delta_pct": -1.0},
        regime="byte-identical", critic_verdict="pass-risk",
        terminal_required=False)
    assert "critic rejected" in bad_critic.reasons
    unstamped = _mf.resolve_mergeability(
        {"delta_pct": -1.0},
        regime="byte-identical", critic_verdict="pass",
        terminal_required=True, terminal="TERMINAL_CONFIRMED")
    assert "unstamped terminal (hand-edited field ignored)" in unstamped.reasons
    not_conf = _mf.resolve_mergeability(
        {"delta_pct": -1.0},
        regime="byte-identical", critic_verdict="pass",
        terminal_required=True,
        terminal_stamp={"verdict": "TERMINAL_UNTOUCHED", "source": "x", "sha256": "y"})
    assert "terminal not stamped-CONFIRMED" in not_conf.reasons
    both = _mf.resolve_mergeability(
        {"delta_pct": -19.15},
        regime="byte-identical", critic_verdict="pass",
        terminal_required=True, terminal="TERMINAL_CONFIRMED",
        outlier_threshold_pct=5.0)
    assert both.mergeable is False
    assert "unstamped terminal (hand-edited field ignored)" in both.reasons
    assert any(r.startswith("outlier:") for r in both.reasons)
    # is_mergeable remains the no-outlier wrapper
    assert _mf.is_mergeable(
        "byte-identical", "pass",
        terminal="TERMINAL_CONFIRMED", terminal_required=True,
        terminal_stamp=stamp_ok) is True
    # (d) quarantine field identical to apply_outlier_quarantine path
    for delta, thr in ((-19.15, 5.0), (-4.5, 5.0), (-5.0, 5.0), (-12.0, 0)):
        e_old = {
            "delta_pct": delta, "regime": "byte-identical",
            "critic_verdict": "pass",
        }
        e_old["mergeable"] = _mf.is_mergeable(
            e_old["regime"], e_old["critic_verdict"])
        _mf.apply_outlier_quarantine(e_old, threshold_pct=thr)
        dec = _mf.resolve_mergeability(
            {"delta_pct": delta},
            regime="byte-identical", critic_verdict="pass",
            terminal_required=False, outlier_threshold_pct=thr)
        e_new = {"delta_pct": delta, "mergeable": dec.mergeable}
        oq = next((r for r in dec.reasons if r.startswith("outlier:")), None)
        if oq:
            e_new["quarantine"] = oq
        assert e_new.get("mergeable") == e_old.get("mergeable"), (delta, thr)
        assert e_new.get("quarantine") == e_old.get("quarantine"), (delta, thr)
        assert ("quarantine" in e_new) == ("quarantine" in e_old)
        # reason string matches the standalone helper byte-for-byte
        assert _mf.outlier_quarantine_reason(delta, thr) == oq


def case_51():
    """T34: quarantine_audit — human clear ruling, staleness latch, rebuild passthrough.

    Hermetic: resolve_mergeability, clear_quarantine API + CLI, build_manifest /
    apply_terminal preservation, no auto-create.
    """
    print("=== case 51: quarantine_audit (clear / refuse / stale / preserve) ===")
    from aro import manifest as _mf
    from aro.cli import build_parser

    def _run(d, delta_pct, *, regime="byte-identical", critic="pass", fn="sload"):
        d = Path(d)
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": fn,
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
        (d / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")

    # --- (1) clear flow: quarantined → audit written; resolve no longer blocks --
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _run(d, -6.404)
        m = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        a0 = m["accepted"][0]
        assert a0["mergeable"] is False
        assert a0.get("quarantine", "").startswith("outlier:")
        assert "quarantine_audit" not in a0
        mp = d / "manifest.json"
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")

        # CLI clear
        args = build_parser().parse_args([
            "manifest", str(d),
            "--clear-quarantine", "1",
            "--by", "alice",
            "--evidence", "reviewed inspect_storage; oracle-complete, clean",
        ])
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            _mf.cli(args)
        man = json.loads(mp.read_text())
        a = man["accepted"][0]
        qa = a["quarantine_audit"]
        assert qa["cleared"] is True
        assert qa["by"] == "alice"
        assert "oracle-complete" in qa["evidence"]
        assert qa["delta_pct"] == -6.404
        assert isinstance(qa["date"], str) and len(qa["date"]) >= 10
        # quarantine provenance kept; mergeable unblocked (no other gates)
        assert a.get("quarantine", "").startswith("outlier:")
        assert a["mergeable"] is True, a
        dec = _mf.resolve_mergeability(
            a, regime="byte-identical", critic_verdict="pass",
            terminal_required=False, outlier_threshold_pct=5.0)
        assert dec.mergeable is True and dec.reasons == []
        assert dec.quarantine_reason and dec.quarantine_reason.startswith("outlier:")
        # re-clear with valid audit → refuse
        try:
            _mf.clear_quarantine(
                man, 1, by="bob", evidence="again",
                outlier_quarantine_pct=5.0)
            raise AssertionError("expected refuse on already-valid audit")
        except SystemExit as se:
            msg = se.args[0] if se.args else ""
            assert "already has a valid" in str(msg), msg
    print("#51a OK: clear-quarantine writes audit; resolve no longer blocks; "
          "quarantine string kept")

    # --- (2) refusals: no quarantine, missing by/evidence, unknown order -------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _run(d, -2.0)  # under threshold → no quarantine
        m = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        assert "quarantine" not in m["accepted"][0]
        mp = d / "manifest.json"
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        try:
            _mf.clear_quarantine(
                m, 1, by="alice", evidence="n/a",
                outlier_quarantine_pct=5.0)
            raise AssertionError("expected refuse when no quarantine")
        except SystemExit as se:
            assert "nothing to clear" in str(se.args[0]), se.args
        try:
            _mf.clear_quarantine(
                m, 99, by="alice", evidence="n/a",
                outlier_quarantine_pct=5.0)
            raise AssertionError("expected refuse for unknown order")
        except SystemExit as se:
            assert "no accepted entry" in str(se.args[0]), se.args

        # CLI missing --by / --evidence → exit 2
        for argv in (
            ["manifest", str(d), "--clear-quarantine", "1",
             "--evidence", "only evidence"],
            ["manifest", str(d), "--clear-quarantine", "1", "--by", "alice"],
        ):
            args = build_parser().parse_args(argv)
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    _mf.cli(args)
                raise AssertionError(f"expected exit 2 for {argv}")
            except SystemExit as se:
                assert se.code == 2, (argv, se.code)
    print("#51b OK: refusals — no quarantine / unknown order / missing by|evidence")

    # --- (3) anti-laundering latch --------------------------------------------
    base_audit = {
        "cleared": True,
        "by": "alice",
        "date": "2026-07-17",
        "evidence": "audited clean",
        "delta_pct": -6.4,
    }
    # within 0.5pp → still cleared
    e_ok = {
        "delta_pct": -6.8,  # |−6.8 − (−6.4)| = 0.4 ≤ 0.5
        "quarantine_audit": dict(base_audit),
        "quarantine": "outlier: |Δ|=6.800% > 5.0%",
    }
    dec_ok = _mf.resolve_mergeability(
        e_ok, regime="byte-identical", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=5.0)
    assert dec_ok.mergeable is True and dec_ok.reasons == []
    assert _mf.is_valid_quarantine_audit(e_ok) is True
    assert _mf.is_stale_quarantine_audit(e_ok) is False
    # drifted beyond 0.5pp → blocked + quarantine-audit-stale
    e_stale = {
        "delta_pct": -8.0,  # |−8.0 − (−6.4)| = 1.6 > 0.5
        "quarantine_audit": dict(base_audit),
        "quarantine": "outlier: |Δ|=8.000% > 5.0%",
    }
    dec_stale = _mf.resolve_mergeability(
        e_stale, regime="byte-identical", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=5.0)
    assert dec_stale.mergeable is False
    assert any(r.startswith("outlier:") for r in dec_stale.reasons)
    assert "quarantine-audit-stale" in dec_stale.reasons
    assert _mf.is_valid_quarantine_audit(e_stale) is False
    assert _mf.is_stale_quarantine_audit(e_stale) is True
    # boundary exactly 0.5pp still valid
    e_edge = {
        "delta_pct": -6.9,  # 0.5 exactly
        "quarantine_audit": dict(base_audit),
    }
    assert _mf.is_valid_quarantine_audit(e_edge) is True
    print("#51c OK: anti-laundering — within 0.5pp clear; beyond → stale marker")

    # --- (4) rebuild preservation (build_manifest + apply_terminal) -----------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _run(d, -6.404)
        m = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        audit = {
            "cleared": True,
            "by": "carol",
            "date": "2026-07-01",
            "evidence": "verbatim payload must survive",
            "delta_pct": -6.404,
        }
        m["accepted"][0]["quarantine_audit"] = dict(audit)
        # re-resolve so mergeable reflects the audit
        _mf._apply_merge_decision(
            m["accepted"][0],
            _mf.resolve_mergeability(
                m["accepted"][0], regime="byte-identical", critic_verdict="pass",
                terminal_required=False, outlier_threshold_pct=5.0))
        assert m["accepted"][0]["mergeable"] is True
        mp = d / "manifest.json"
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")

        # build_manifest rebuild carries audit verbatim
        m2 = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        assert m2["accepted"][0].get("quarantine_audit") == audit, m2["accepted"][0]
        assert m2["accepted"][0]["mergeable"] is True
        assert m2["accepted"][0].get("quarantine", "").startswith("outlier:")

        # apply_terminal rebuild also leaves audit untouched
        fake_term = {
            "verdict": "TERMINAL_UNTOUCHED",
            "bench_ir_rows": {},
            "profile_fingerprint": "fp",
        }
        m3 = _mf.apply_terminal(
            m2, fake_term, terminal_required=False, outlier_quarantine_pct=5.0)
        assert m3["accepted"][0].get("quarantine_audit") == audit
    print("#51d OK: rebuild preserves quarantine_audit verbatim "
          "(build_manifest + apply_terminal)")

    # --- (5) no auto-create on rebuild without prior audit --------------------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _run(d, -19.15)
        m = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        assert m["accepted"][0]["mergeable"] is False
        assert "quarantine" in m["accepted"][0]
        assert "quarantine_audit" not in m["accepted"][0]
        mp = d / "manifest.json"
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        m2 = _mf.build_manifest(d, outlier_quarantine_pct=5.0)
        assert "quarantine_audit" not in m2["accepted"][0]
        m3 = _mf.apply_terminal(
            m2,
            {"verdict": "TERMINAL_UNTOUCHED", "bench_ir_rows": {},
             "profile_fingerprint": None},
            terminal_required=False, outlier_quarantine_pct=5.0)
        assert "quarantine_audit" not in m3["accepted"][0]
    print("#51e OK: no auto-create of quarantine_audit on rebuild")

    # --- (6) argparse surface for the clear flags ------------------------------
    p = build_parser()
    a = p.parse_args([
        "manifest", "/tmp/out",
        "--clear-quarantine", "11",
        "--by", "dave",
        "--evidence", "clean",
    ])
    assert a.cmd == "manifest" and a.clear_quarantine == 11
    assert a.by == "dave" and a.evidence == "clean"
    # clear_quarantine also works via SimpleNamespace dispatch
    ns = SimpleNamespace(
        out_dir="/nope", out=None, spec=None, terminal=None,
        clear_quarantine=1, by="", evidence="x")
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            _mf.cli(ns)
        raise AssertionError("expected exit 2 for empty --by")
    except SystemExit as se:
        assert se.code == 2
    print("#51f OK: CLI flags parse; empty --by refused")
    print("case 51 OK")

