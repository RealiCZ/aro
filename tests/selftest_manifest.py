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


def case_52():
    """T37: reverify dimension + regime waiver + rejudge --update-manifest.

    Hermetic: resolve_mergeability / apply_terminal / reverify passthrough,
    quarantine_audit interplay, terminal CLI rejudge write-back.
    """
    print("=== case 52: reverify dimension + regime waiver + rejudge write-back ===")
    from aro import manifest as _mf
    from aro import terminal as _tm
    from aro.cli import build_parser

    term_confirmed = {
        "verdict": "TERMINAL_CONFIRMED",
        "bench_ir_rows": {"row": -2.0},
        "profile_fingerprint": "fp-t37",
    }

    # --- Gap A: reverify fail blocks; survives apply_terminal; no-stamp legacy --
    e_fail = {
        "delta_pct": -1.0,
        "reverify": {"verdict": "reverify-fail: test_full"},
    }
    dec_fail = _mf.resolve_mergeability(
        e_fail, regime="byte-identical", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=0)
    assert dec_fail.mergeable is False
    assert any(r.startswith("reverify:") for r in dec_fail.reasons), dec_fail.reasons
    assert "reverify-fail: test_full" in dec_fail.reasons[0] or any(
        "reverify-fail: test_full" in r for r in dec_fail.reasons)

    # apply_terminal with CONFIRMED must NOT resurrect
    man = {
        "accepted": [{
            "order": 1, "id": "c1", "regime": "byte-identical",
            "critic_verdict": "pass", "delta_pct": -1.0,
            "mergeable": False,
            "reverify": {"verdict": "reverify-fail: test_full",
                         "failing_gate": "test_full"},
        }],
    }
    # Write a real terminal.json for stamp integrity path
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # Minimal rows so apply_terminal without source still re-resolves
        m2 = _mf.apply_terminal(
            man, term_confirmed, terminal_required=False,
            outlier_quarantine_pct=0)
        assert m2["accepted"][0]["mergeable"] is False, m2["accepted"][0]
        assert m2["accepted"][0].get("reverify", {}).get("verdict") == (
            "reverify-fail: test_full")
        # With terminal_required + stamp source still blocked by reverify
        tpath = d / "terminal.json"
        # Build a real verifiable terminal doc
        def _mdoc(rows, fp="fp-t37"):
            return _tm.MeasureDoc(
                rows=dict(rows), meta={"profile_fingerprint": fp},
                profile_fingerprint=fp, rustc="rustc 1.80")
        r_ok = _tm.judge_terminal(
            _mdoc({"a": 10000}), _mdoc({"a": 9000}), epsilon_pct=0.1)
        tpath.write_text(
            json.dumps(r_ok.to_dict(), ensure_ascii=False, indent=1) + "\n")
        man_t = {
            "accepted": [{
                "order": 1, "id": "c1", "regime": "byte-identical",
                "critic_verdict": "pass", "delta_pct": -1.0,
                "mergeable": False,
                "reverify": {"verdict": "reverify-fail: test_full"},
            }],
        }
        m3 = _mf.apply_terminal(
            man_t, r_ok, terminal_required=True, source=str(tpath),
            outlier_quarantine_pct=0, control_lanes=[])
        assert m3["accepted"][0]["mergeable"] is False, m3["accepted"][0]
        assert any(
            r.startswith("reverify:") for r in _mf.resolve_mergeability(
                m3["accepted"][0],
                regime="byte-identical", critic_verdict="pass",
                terminal_required=True,
                terminal_stamp=m3["accepted"][0].get("terminal_stamp"),
                outlier_threshold_pct=0,
            ).reasons)

    # No reverify stamp → unaffected (legacy mergeable when other gates green)
    dec_legacy = _mf.resolve_mergeability(
        {"delta_pct": -1.0},
        regime="byte-identical", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=0)
    assert dec_legacy.mergeable is True and dec_legacy.reasons == []
    assert dec_legacy.regime_waived_by_reverify is False
    print("#52a OK: reverify-fail blocks + survives apply_terminal; "
          "no-stamp legacy unaffected")

    # --- Gap B: regime waiver on reverify-pass ---------------------------------
    e_relaxed_pass = {
        "delta_pct": -2.0,
        "reverify": {"verdict": "reverify-pass"},
    }
    dec_waive = _mf.resolve_mergeability(
        e_relaxed_pass, regime="relaxed", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=0)
    assert dec_waive.mergeable is True, dec_waive
    assert dec_waive.reasons == []
    assert dec_waive.regime_waived_by_reverify is True
    e_stamp = dict(e_relaxed_pass)
    _mf._apply_merge_decision(e_stamp, dec_waive)
    assert e_stamp["mergeable"] is True
    assert e_stamp.get("regime_waiver") == "reverify-pass"
    # regime field is caller/provenance — not rewritten by resolve
    # (entry itself may not carry regime; waiver is the decision marker)

    # relaxed without reverify → still blocked
    dec_relaxed = _mf.resolve_mergeability(
        {"delta_pct": -2.0},
        regime="relaxed", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=0)
    assert dec_relaxed.mergeable is False
    assert "regime not byte-identical" in dec_relaxed.reasons
    assert dec_relaxed.regime_waived_by_reverify is False
    e_no = {"delta_pct": -2.0}
    _mf._apply_merge_decision(e_no, dec_relaxed)
    assert "regime_waiver" not in e_no

    # byte-identical + reverify-pass → mergeable, NO waiver stamp
    dec_bi = _mf.resolve_mergeability(
        {"delta_pct": -1.0, "reverify": {"verdict": "reverify-pass"}},
        regime="byte-identical", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=0)
    assert dec_bi.mergeable is True
    assert dec_bi.regime_waived_by_reverify is False
    e_bi = {"delta_pct": -1.0, "reverify": {"verdict": "reverify-pass"}}
    _mf._apply_merge_decision(e_bi, dec_bi)
    assert "regime_waiver" not in e_bi
    print("#52b OK: relaxed+reverify-pass waives; relaxed alone blocks; "
          "byte-identical no waiver stamp")

    # --- Interplay with T34: quarantine_audit + reverify -----------------------
    base_audit = {
        "cleared": True,
        "by": "alice",
        "date": "2026-07-17",
        "evidence": "oracle-complete",
        "delta_pct": -6.4,
    }
    e_both_ok = {
        "delta_pct": -6.4,
        "quarantine": "outlier: |Δ|=6.400% > 5.0%",
        "quarantine_audit": dict(base_audit),
        "reverify": {"verdict": "reverify-pass"},
    }
    dec_both = _mf.resolve_mergeability(
        e_both_ok, regime="relaxed", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=5.0)
    assert dec_both.mergeable is True, dec_both
    assert dec_both.regime_waived_by_reverify is True
    assert dec_both.quarantine_reason and dec_both.quarantine_reason.startswith(
        "outlier:")
    e_both_stamped = dict(e_both_ok)
    _mf._apply_merge_decision(e_both_stamped, dec_both)
    assert e_both_stamped["mergeable"] is True
    assert e_both_stamped.get("regime_waiver") == "reverify-pass"
    assert e_both_stamped.get("quarantine_audit") == base_audit
    assert e_both_stamped.get("quarantine", "").startswith("outlier:")

    # same but reverify-fail → false even with valid audit
    e_rev_fail = {
        "delta_pct": -6.4,
        "quarantine": "outlier: |Δ|=6.400% > 5.0%",
        "quarantine_audit": dict(base_audit),
        "reverify": {"verdict": "reverify-fail", "failing_gate": "differential"},
    }
    dec_rf = _mf.resolve_mergeability(
        e_rev_fail, regime="relaxed", critic_verdict="pass",
        terminal_required=False, outlier_threshold_pct=5.0)
    assert dec_rf.mergeable is False
    assert any(r.startswith("reverify:") for r in dec_rf.reasons)
    # reverify-fail does not waive regime either (not reverify-pass)
    assert "regime not byte-identical" in dec_rf.reasons
    print("#52c OK: quarantine_audit + reverify-pass both clear; "
          "reverify-fail wins over audit")

    # --- build_manifest passthrough of reverify (no resurrection on rebuild) ---
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0",
             "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -1.0, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        m = _mf.build_manifest(d, outlier_quarantine_pct=0)
        assert m["accepted"][0]["mergeable"] is True
        m["accepted"][0]["reverify"] = {
            "verdict": "reverify-fail", "failing_gate": "test_full"}
        _mf._apply_merge_decision(
            m["accepted"][0],
            _mf.resolve_mergeability(
                m["accepted"][0], regime="byte-identical",
                critic_verdict="pass", terminal_required=False,
                outlier_threshold_pct=0))
        assert m["accepted"][0]["mergeable"] is False
        (d / "manifest.json").write_text(
            json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        m2 = _mf.build_manifest(d, outlier_quarantine_pct=0)
        assert m2["accepted"][0].get("reverify", {}).get("verdict") == (
            "reverify-fail")
        assert m2["accepted"][0]["mergeable"] is False
    print("#52d OK: build_manifest carries reverify; demotion survives rebuild")

    # --- Gap C: --rejudge --update-manifest write-back -------------------------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        def _mdoc(rows, fp="fp-t37c"):
            return _tm.MeasureDoc(
                rows=dict(rows), meta={"profile_fingerprint": fp},
                profile_fingerprint=fp, rustc="rustc 1.80")

        r_ok = _tm.judge_terminal(
            _mdoc({"a": 10000, "b": 20000}),
            _mdoc({"a": 9000, "b": 20000}),
            epsilon_pct=0.1)
        assert r_ok.verdict == _tm.TERMINAL_CONFIRMED
        term_path = d / "terminal.json"
        term_path.write_text(
            json.dumps(r_ok.to_dict(), ensure_ascii=False, indent=1) + "\n")

        # Minimal events + patch for an existing manifest entry
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
            {"event": "attempt_started", "run_id": "R", "fn": "sload",
             "regime": "byte-identical", "files": ["crates/x/src/b.rs"]},
            {"event": "candidate_proposed", "run_id": "R", "id": "agent-r0-0",
             "hypothesis": "hoist"},
            {"event": "critic", "run_id": "R", "id": "agent-r0-0",
             "verdict": "pass"},
            {"event": "candidate_verdict", "run_id": "R", "id": "agent-r0-0",
             "deltas": [{"metric": "ns", "delta_pct": -4.5, "improved": True}]},
            {"event": "baseline_advanced", "run_id": "R", "by": "agent-r0-0"},
        ]
        (d / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in evs) + "\n")
        pd = d / "a1" / "patches"
        pd.mkdir(parents=True)
        (pd / "agent-r0-0.txt").write_text(
            "--- edit 1 ---\npath: crates/x/src/b.rs\n"
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        m = _mf.build_manifest(d, outlier_quarantine_pct=0)
        # Pre-stamp as unstamped / mergeable false under terminal_required
        m["accepted"][0]["mergeable"] = False
        m["accepted"][0].pop("terminal_stamp", None)
        m["accepted"][0].pop("terminal", None)
        (d / "manifest.json").write_text(
            json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        before = json.loads((d / "manifest.json").read_text())
        assert "terminal_stamp" not in before["accepted"][0]

        spec_path = d / "spec.json"
        spec_path.write_text(json.dumps({
            "name": "t37-rejudge",
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
            "outlier_quarantine_pct": 0,
        }))

        # rejudge WITHOUT --update-manifest: .rejudged.json written, manifest intact
        rejudged_path = Path(str(term_path) + ".rejudged.json")
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            _tm.cli(SimpleNamespace(
                spec=str(spec_path), rejudge=str(term_path),
                list=False, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=None, hypothesis=None, events_ref=None,
                calibrate=False))
        assert rejudged_path.is_file(), "rejudge must write .rejudged.json"
        mid = json.loads((d / "manifest.json").read_text())
        assert "terminal_stamp" not in mid["accepted"][0]
        rejudged_path.unlink()

        # rejudge WITH --update-manifest: stamp via apply_terminal path
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            _tm.cli(SimpleNamespace(
                spec=str(spec_path), rejudge=str(term_path),
                list=False, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=str(d), hypothesis=None,
                events_ref=None, calibrate=False))
        assert rejudged_path.is_file()
        after = json.loads((d / "manifest.json").read_text())
        a0 = after["accepted"][0]
        assert a0.get("terminal") == "TERMINAL_CONFIRMED", a0
        assert isinstance(a0.get("terminal_stamp"), dict), a0
        assert a0["terminal_stamp"]["verdict"] == "TERMINAL_CONFIRMED"
        assert a0["terminal_stamp"]["source"] == str(rejudged_path)
        assert a0.get("mergeable") is True, a0
        assert after.get("terminal", {}).get("verdict") == "TERMINAL_CONFIRMED"

    # argparse: --update-manifest still available with --rejudge; mutex intact
    p = build_parser()
    a = p.parse_args([
        "terminal", "targets/x.json",
        "--rejudge", "/tmp/t.json",
        "--update-manifest", "/tmp/run",
    ])
    assert a.rejudge == "/tmp/t.json" and a.update_manifest == "/tmp/run"
    try:
        p.parse_args([
            "terminal", "targets/x.json",
            "--calibrate", "--rejudge", "x.json",
        ])
        raise AssertionError("expected argparse error for --calibrate --rejudge")
    except SystemExit as se:
        assert se.code == 2
    try:
        p.parse_args([
            "terminal", "targets/x.json",
            "--rejudge", "x.json", "--list",
        ])
        raise AssertionError("expected argparse error for --rejudge --list")
    except SystemExit as se:
        assert se.code == 2
    print("#52e OK: --rejudge --update-manifest stamps via apply_terminal; "
          "without flag unchanged; mutex intact")
    print("case 52 OK")


def case_53():
    """T38: measured-set membership — terminal stamps only cover measured orders.

    Hermetic: apply_terminal membership resolution, stamp removal, parse_orders
    ranges, terminal_doc_dict, CLI --rejudge --update-manifest --orders.
    """
    print("=== case 53: terminal measured-set membership ===")
    from aro import manifest as _mf
    from aro import terminal as _tm
    from aro.cli import build_parser
    from aro.reverify import parse_orders

    def _mdoc(rows, fp="fp-t38"):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": fp},
            profile_fingerprint=fp, rustc="rustc 1.80")

    r_ok = _tm.judge_terminal(
        _mdoc({"a": 10000, "b": 20000}),
        _mdoc({"a": 9000, "b": 20000}),
        epsilon_pct=0.1)
    assert r_ok.verdict == _tm.TERMINAL_CONFIRMED
    base_doc = r_ok.to_dict()

    def _three_entry_manifest(*, stale_stamp_on=None):
        """3 accepted entries, all otherwise mergeable (byte-identical + pass)."""
        accepted = []
        for i in (1, 2, 3):
            a = {
                "order": i,
                "id": f"c{i}",
                "regime": "byte-identical",
                "critic_verdict": "pass",
                "delta_pct": -1.0 - i * 0.1,
                "mergeable": False,
            }
            if stale_stamp_on is not None and i in stale_stamp_on:
                a["terminal_stamp"] = {
                    "verdict": "TERMINAL_CONFIRMED",
                    "source": "/stale/old.json",
                    "sha256": "deadbeef",
                }
                a["terminal"] = "TERMINAL_CONFIRMED"
            accepted.append(a)
        return {"accepted": accepted}

    # --- (1) Membership from doc.measured_orders ------------------------------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tpath = d / "terminal.json"
        doc = dict(base_doc)
        doc["measured_orders"] = [1, 3]
        tpath.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        m = _three_entry_manifest()
        m = _mf.apply_terminal(
            m, doc, terminal_required=True, source=str(tpath),
            outlier_quarantine_pct=0, control_lanes=[])
        a1, a2, a3 = m["accepted"]
        assert a1.get("terminal") == "TERMINAL_CONFIRMED", a1
        assert isinstance(a1.get("terminal_stamp"), dict), a1
        assert a1["mergeable"] is True, a1
        assert a3.get("terminal") == "TERMINAL_CONFIRMED", a3
        assert isinstance(a3.get("terminal_stamp"), dict), a3
        assert a3["mergeable"] is True, a3
        assert a2.get("terminal") == _tm.TERMINAL_NOT_MEASURED, a2
        assert "terminal_stamp" not in a2, a2
        assert a2["mergeable"] is False, a2
        dec2 = _mf.resolve_mergeability(
            a2, regime="byte-identical", critic_verdict="pass",
            terminal_required=True, terminal_stamp=a2.get("terminal_stamp"),
            terminal=a2.get("terminal"), outlier_threshold_pct=0)
        assert dec2.mergeable is False
        assert any("unstamped" in r for r in dec2.reasons), dec2.reasons
        assert m.get("terminal", {}).get("measured_orders") == [1, 3]
    print("#53a OK: doc measured_orders stamps 1,3; entry 2 TERMINAL_NOT_MEASURED")

    # --- (2) Explicit orders param; beats doc when both present ---------------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tpath = d / "terminal.json"
        # Doc claims all three; explicit orders wins → only 1,3
        doc = dict(base_doc)
        doc["measured_orders"] = [1, 2, 3]
        tpath.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        m = _three_entry_manifest()
        m = _mf.apply_terminal(
            m, doc, terminal_required=True, source=str(tpath),
            outlier_quarantine_pct=0, control_lanes=[],
            orders=[1, 3])
        assert m["accepted"][0]["mergeable"] is True
        assert m["accepted"][1]["terminal"] == _tm.TERMINAL_NOT_MEASURED
        assert "terminal_stamp" not in m["accepted"][1]
        assert m["accepted"][1]["mergeable"] is False
        assert m["accepted"][2]["mergeable"] is True
        # Doc without measured_orders + explicit orders → same
        doc2 = dict(base_doc)
        tpath2 = d / "terminal2.json"
        tpath2.write_text(json.dumps(doc2, ensure_ascii=False, indent=1) + "\n")
        m2 = _three_entry_manifest()
        m2 = _mf.apply_terminal(
            m2, doc2, terminal_required=True, source=str(tpath2),
            outlier_quarantine_pct=0, control_lanes=[],
            orders=[1, 3])
        assert m2["accepted"][0]["mergeable"] is True
        assert m2["accepted"][1]["terminal"] == _tm.TERMINAL_NOT_MEASURED
        assert m2["accepted"][2]["mergeable"] is True
    print("#53b OK: explicit orders; explicit beats doc measured_orders")

    # --- (3) Legacy: no param, no doc field → all stamped ---------------------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tpath = d / "terminal.json"
        doc = dict(base_doc)
        assert "measured_orders" not in doc
        tpath.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        m = _three_entry_manifest()
        m = _mf.apply_terminal(
            m, doc, terminal_required=True, source=str(tpath),
            outlier_quarantine_pct=0, control_lanes=[])
        for a in m["accepted"]:
            assert a.get("terminal") == "TERMINAL_CONFIRMED", a
            assert isinstance(a.get("terminal_stamp"), dict), a
            assert a["mergeable"] is True, a
        assert "measured_orders" not in m.get("terminal", {})
    print("#53c OK: legacy all-stamped (byte-compatible with pre-T38)")

    # --- (4) Stamp removal for out-of-set entry with stale prior stamp --------
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        tpath = d / "terminal.json"
        doc = dict(base_doc)
        doc["measured_orders"] = [1, 3]
        tpath.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        m = _three_entry_manifest(stale_stamp_on={2})
        assert "terminal_stamp" in m["accepted"][1]
        m = _mf.apply_terminal(
            m, doc, terminal_required=True, source=str(tpath),
            outlier_quarantine_pct=0, control_lanes=[])
        a2 = m["accepted"][1]
        assert "terminal_stamp" not in a2, a2
        assert a2["terminal"] == _tm.TERMINAL_NOT_MEASURED
        assert a2["mergeable"] is False
    print("#53d OK: stale terminal_stamp removed for out-of-set entry")

    # --- (5) parse_orders ranges + CLI surface --------------------------------
    assert parse_orders("1,3,8") == {1, 3, 8}
    assert parse_orders("1-13") == set(range(1, 14))
    assert parse_orders("1,3,5-8") == {1, 3, 5, 6, 7, 8}
    assert parse_orders(None) is None
    assert parse_orders("") is None
    try:
        parse_orders("1-")
        raise AssertionError("expected ValueError for open range")
    except ValueError:
        pass
    try:
        parse_orders("5-3")
        raise AssertionError("expected ValueError for lo>hi")
    except ValueError:
        pass
    try:
        parse_orders("nope")
        raise AssertionError("expected ValueError for non-int")
    except ValueError:
        pass

    # terminal_doc_dict embeds measured_orders when given
    emb = _tm.terminal_doc_dict(r_ok, measured_orders={3, 1})
    assert emb["measured_orders"] == [1, 3]
    emb_legacy = _tm.terminal_doc_dict(r_ok, measured_orders=None)
    assert "measured_orders" not in emb_legacy

    # argparse accepts --orders with --rejudge / measure
    p = build_parser()
    a = p.parse_args([
        "terminal", "targets/x.json",
        "--rejudge", "/tmp/t.json",
        "--update-manifest", "/tmp/run",
        "--orders", "1-13",
    ])
    assert a.orders == "1-13" and a.rejudge == "/tmp/t.json"
    a2 = p.parse_args([
        "terminal", "targets/x.json",
        "--baseline", "/b", "--candidate", "/c",
        "--orders", "1,3",
    ])
    assert a2.orders == "1,3"

    # CLI: invalid --orders surfaces cleanly
    try:
        _tm.cli(SimpleNamespace(
            spec="x", rejudge=None, list=False, dry_run=False,
            baseline=None, candidate=None, out=None, record=False,
            fn=None, update_manifest=None, hypothesis=None, events_ref=None,
            calibrate=False, orders="bad-range-"))
        raise AssertionError("expected SystemExit for bad --orders")
    except SystemExit as se:
        msg = str(se)
        assert "orders" in msg.lower() or se.code not in (0, None), msg

    # CLI rejudge --update-manifest --orders end-to-end
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        term_path = d / "terminal.json"
        # Doc without measured_orders (like terminal-r3.json) — explicit --orders
        term_path.write_text(
            json.dumps(base_doc, ensure_ascii=False, indent=1) + "\n")

        # Three-entry run dir + pre-built manifest
        evs = [
            {"event": "run_started", "run_id": "R", "target": "demo",
             "baseline_ref": "abc123"},
        ]
        for i in (1, 2, 3):
            evs.extend([
                {"event": "attempt_started", "run_id": "R", "fn": f"f{i}",
                 "regime": "byte-identical", "files": [f"src/f{i}.rs"]},
                {"event": "candidate_proposed", "run_id": "R", "id": f"c{i}",
                 "hypothesis": "h"},
                {"event": "critic", "run_id": "R", "id": f"c{i}",
                 "verdict": "pass"},
                {"event": "candidate_verdict", "run_id": "R", "id": f"c{i}",
                 "deltas": [{"metric": "ns", "delta_pct": -1.0,
                             "improved": True}]},
                {"event": "baseline_advanced", "run_id": "R", "by": f"c{i}"},
            ])
        (d / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in evs) + "\n")
        for i in (1, 2, 3):
            pd = d / f"a{i}" / "patches"
            pd.mkdir(parents=True)
            (pd / f"c{i}.txt").write_text(
                f"--- edit 1 ---\npath: src/f{i}.rs\n"
                "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        man = _mf.build_manifest(d, outlier_quarantine_pct=0)
        assert len(man["accepted"]) == 3
        # Give entry 2 a stale stamp that must be cleared
        man["accepted"][1]["terminal_stamp"] = {
            "verdict": "TERMINAL_CONFIRMED",
            "source": "old", "sha256": "x"}
        man["accepted"][1]["terminal"] = "TERMINAL_CONFIRMED"
        man["accepted"][1]["mergeable"] = True
        (d / "manifest.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=1) + "\n")

        spec_path = d / "spec.json"
        spec_path.write_text(json.dumps({
            "name": "t38-rejudge",
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
            "outlier_quarantine_pct": 0,
        }))

        rejudged = Path(str(term_path) + ".rejudged.json")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            _tm.cli(SimpleNamespace(
                spec=str(spec_path), rejudge=str(term_path),
                list=False, dry_run=False,
                baseline=None, candidate=None, out=None, record=False,
                fn=None, update_manifest=str(d), hypothesis=None,
                events_ref=None, calibrate=False, orders="1,3"))
        assert rejudged.is_file()
        rj = json.loads(rejudged.read_text())
        assert rj.get("measured_orders") == [1, 3], rj
        after = json.loads((d / "manifest.json").read_text())
        assert after["accepted"][0]["mergeable"] is True
        assert after["accepted"][0].get("terminal_stamp", {}).get(
            "verdict") == "TERMINAL_CONFIRMED"
        assert after["accepted"][1]["terminal"] == _tm.TERMINAL_NOT_MEASURED
        assert "terminal_stamp" not in after["accepted"][1]
        assert after["accepted"][1]["mergeable"] is False
        assert after["accepted"][2]["mergeable"] is True

        # measure-path: terminal_doc_dict records --orders (no real measure)
        md = _tm.terminal_doc_dict(r_ok, measured_orders=parse_orders("1-3"))
        assert md["measured_orders"] == [1, 2, 3]
    print("#53e OK: parse_orders ranges; CLI rejudge --orders write-back; "
          "doc-write helper records membership")
    print("case 53 OK")

