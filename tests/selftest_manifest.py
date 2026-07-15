from __future__ import annotations

import json
import tempfile
from pathlib import Path

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
    print("#27 OK: manifest resolves id-collision by attempt + flags only clean byte-identical mergeable")

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

