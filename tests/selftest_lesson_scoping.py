"""T51: lesson relevance scoping + tried-bucket freshness.

Polarity: lessons inform cheaply; only strong evidence suppresses the frontier.
Hermetic — temp lesson files, fake freshness, no cargo/network.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import aro.frontier as _fr
import aro.lessons as _lm


def case_67():
    print("=== case 67: lesson scoping + tried-bucket freshness (T51) ===")

    # --- (1) _relevant: exact match only; salt ∉ salt-msm; other-repo out ---
    assert _lm._relevant("salt-msm", "salt-msm") is True
    assert _lm._relevant("salt", "salt-msm") is False, "token overlap must die"
    assert _lm._relevant("salt/banderwagon", "salt-msm") is False
    assert _lm._relevant("*", "salt-msm") is True
    assert _lm._relevant("salt-ipa", "salt-msm",
                         lesson_repo="/tmp/salt", target_repo="/tmp/salt") is True
    assert _lm._relevant("mega-evm-v2", "salt-msm",
                         lesson_repo="/tmp/mega", target_repo="/tmp/salt") is False
    # Same-repo without stamps never invents a match.
    assert _lm._relevant("salt-ipa", "salt-msm") is False

    # --- (2) recent() indexes exact salt-msm; excludes ancient salt + other-repo ---
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "lessons.jsonl"
        rows = [
            {"target": "salt", "change": "add_affine_point mul reduce",
             "verdict": "within-noise", "note": "ancient", "delta_pct": -0.5},
            {"target": "salt/banderwagon", "change": "mul_index batch",
             "verdict": "regressed", "note": "PR3 wall-clock", "delta_pct": 50.0},
            {"target": "salt-msm", "change": "mul_with_table into mul_index",
             "verdict": "within-noise", "note": "real msm lesson", "delta_pct": -1.0,
             "baseline_sha": "abc123", "repo": "/tmp/salt"},
            {"target": "mega-evm-v2", "change": "sstore gas", "verdict": "rejected",
             "note": "other repo", "repo": "/tmp/mega"},
            {"target": "*", "change": "MEASUREMENT: shared CARGO_TARGET_DIR",
             "verdict": "measurement-unsound", "note": "global hygiene"},
            {"target": "salt-ipa", "change": "ipa tip", "verdict": "accepted",
             "note": "same-repo sibling", "repo": "/tmp/salt"},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        old = _lm._PATH
        _lm._PATH = path
        try:
            got = _lm.recent("salt-msm", limit=50, repo="/tmp/salt")
            targets = {r["target"] for r in got}
            assert "salt" not in targets and "salt/banderwagon" not in targets, targets
            assert "mega-evm-v2" not in targets, targets
            assert "salt-msm" in targets and "*" in targets and "salt-ipa" in targets, targets

            # Prompt summary still carries informational same-repo history.
            dig = _lm.summary("salt-msm", repo="/tmp/salt")
            assert "mul_with_table" in dig and "ipa tip" in dig
            assert "add_affine_point mul reduce" not in dig  # other-target no repo stamp
        finally:
            _lm._PATH = old

    # --- (3) tried-bucket matrix ---
    ranked = [
        ("add_affine_point", 12.0, "11banderwagon4salt_add"),
        ("mul_index", 8.0, "11banderwagon4salt_mul"),
        ("fresh_fn", 5.0, "11banderwagon4salt_fresh"),
        ("stale_fn", 4.0, "11banderwagon4salt_stale"),
        ("legacy_fn", 3.0, "11banderwagon4salt_leg"),
        ("cross_fn", 2.5, "11banderwagon4salt_cross"),
    ]
    # Production-shaped 4-tuples from _lesson_index.
    idx = [
        ("touch add_affine_point arithmetic", "within-noise", False,
         {"source": "salt-msm", "same_target": True, "baseline_sha": "b_fresh"}),
        ("mul_index layout", "regressed", False,
         {"source": "salt-msm", "same_target": True, "baseline_sha": "b_fresh"}),
        ("stale_fn rewrite", "within-noise", False,
         {"source": "salt-msm", "same_target": True, "baseline_sha": "b_stale"}),
        ("legacy_fn freeform", "within-noise", False,
         {"source": "salt-msm", "same_target": True, "baseline_sha": None}),
        ("cross_fn from salt", "within-noise", False,
         {"source": "salt", "same_target": False, "baseline_sha": "b_fresh"}),
    ]

    def _fresh(repo, sha, files, head_ref="HEAD"):
        return sha == "b_fresh"

    def _locate(name, symbol=""):
        return [f"src/{name}.rs"]

    bk = _fr.bucket_functions(
        ranked, "salt", idx, min_pct=1.0,
        repo="/tmp/salt", head_ref="HEAD",
        locate=_locate, fresh_check=_fresh,
    )
    untried = {r["name"] for r in bk["untried"]}
    tried = {r["name"] for r in bk["tried"]}
    # same-target + fresh + stamped → tried (today preserved)
    assert "add_affine_point" in tried and "mul_index" in tried, bk
    # stale / unstamped / cross-target → untried
    assert "stale_fn" in untried and "legacy_fn" in untried and "cross_fn" in untried, untried
    assert "fresh_fn" in untried
    reasons = {(d["fn"], d["reason"]) for d in bk["lesson_downgraded"]}
    assert ("stale_fn", "stale") in reasons, reasons
    assert ("legacy_fn", "unstamped") in reasons, reasons
    assert ("cross_fn", "cross-target") in reasons, reasons

    # 3-tuple legacy path still suppresses on name match (direct callers / older tests).
    bk3 = _fr.bucket_functions(
        [("check_limit", 3.0, "token_t_check")], "token_t",
        [("check_limit fan-out", "within-noise", False)], min_pct=1.0)
    assert [r["name"] for r in bk3["tried"]] == ["check_limit"]

    # --- (4) writer records baseline_sha + repo ---
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "lessons.jsonl"
        old = _lm._PATH
        _lm._PATH = path
        try:
            _lm.append("salt-msm", "inline hot path", "within-noise", -0.2,
                       "note", baseline_sha="deadbeef", repo="/tmp/salt")
            _lm.append("salt-msm", "legacy shape", "ok")  # no stamp
            written = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
            assert written[0]["baseline_sha"] == "deadbeef"
            assert written[0]["repo"]  # normalized absolute
            assert "baseline_sha" not in written[1] and "repo" not in written[1]
        finally:
            _lm._PATH = old

    # --- (5) Frontier e2e: salt scenario → both fns untried ---
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "lessons.jsonl"
        ancient = [
            {"target": "salt/banderwagon",
             "change": "precompute-K: add_affine_point 10->8 muls",
             "verdict": "accepted", "note": "PR3 wall-clock", "delta_pct": -14.1},
            {"target": "salt/banderwagon",
             "change": "remove heap Vec in mul_index",
             "verdict": "regressed", "note": "BIG REGRESSION", "delta_pct": 53.6},
            {"target": "salt",
             "change": "add_affine_point strength-reduce",
             "verdict": "within-noise", "note": "sub-noise", "delta_pct": -0.62},
            {"target": "salt",
             "change": "mul_index fuse loops",
             "verdict": "regressed", "note": "ancient", "delta_pct": 10.0},
        ]
        path.write_text("\n".join(json.dumps(r) for r in ancient) + "\n")
        old = _lm._PATH
        _lm._PATH = path
        try:
            idx = _fr._lesson_index("salt-msm", repo="/tmp/salt")
            # Ancient rows must NOT be indexed (no exact/same-repo stamp).
            assert idx == [] or all(
                not e[3]["same_target"] for e in idx if len(e) >= 4), idx
            ranked = [
                ("add_affine_point", 20.0, "11banderwagon4salt_add"),
                ("mul_index", 15.0, "11banderwagon4salt_mul"),
            ]
            # Even if we force-index ancient text as cross-target (simulating a
            # future same-repo stamp), they must not suppress.
            forced = [
                ("precompute add_affine_point", "accepted", False,
                 {"source": "salt/banderwagon", "same_target": False,
                  "baseline_sha": "old"}),
                ("mul_index fuse", "regressed", False,
                 {"source": "salt", "same_target": False, "baseline_sha": "old"}),
            ]
            bk = _fr.bucket_functions(
                ranked, "salt", forced, min_pct=1.0,
                fresh_check=lambda *a, **k: True,
                locate=lambda n, s="": [f"src/{n}.rs"])
            names = {r["name"] for r in bk["untried"]}
            assert names == {"add_affine_point", "mul_index"}, bk
            assert not bk["tried"] and not bk["gated"]
        finally:
            _lm._PATH = old

    # --- (6) Prompt seam: informational lessons still reach generator context ---
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "lessons.jsonl"
        path.write_text(json.dumps({
            "target": "salt-ipa", "change": "sibling tip on msm math",
            "verdict": "accepted", "note": "informational", "repo": "/work/salt",
        }) + "\n" + json.dumps({
            "target": "salt-msm", "change": "exact target lesson",
            "verdict": "within-noise", "note": "exact", "repo": "/work/salt",
            "baseline_sha": "sha1",
        }) + "\n")
        old = _lm._PATH
        _lm._PATH = path
        try:
            # Generator seam is lessons.summary(target, repo=...) — assert there.
            text = _lm.summary("salt-msm", repo="/work/salt")
            assert "sibling tip on msm math" in text
            assert "exact target lesson" in text
            # And the AgenticGenerator._lessons path uses the same helper.
            from aro.generator import AgenticGenerator

            class _T:
                name = "salt-msm"

                class spec:
                    repo = "/work/salt"

            g = AgenticGenerator.__new__(AgenticGenerator)
            g.target = _T()
            assert "sibling tip" in g._lessons()
        finally:
            _lm._PATH = old

    # --- (7) _lesson_index meta flags for same-target / unstamped ---
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "lessons.jsonl"
        path.write_text("\n".join([
            json.dumps({"target": "salt-msm", "change": "x", "verdict": "ok",
                        "note": "", "baseline_sha": "s1", "repo": "/r"}),
            json.dumps({"target": "salt-msm", "change": "y", "verdict": "ok",
                        "note": ""}),  # unstamped
            json.dumps({"target": "salt-ipa", "change": "z", "verdict": "ok",
                        "note": "", "repo": "/r"}),
        ]) + "\n")
        old = _lm._PATH
        _lm._PATH = path
        try:
            idx = _fr._lesson_index("salt-msm", repo="/r")
            assert len(idx) == 3
            metas = [e[3] for e in idx]
            assert metas[0]["same_target"] is True and metas[0]["baseline_sha"] == "s1"
            assert metas[1]["same_target"] is True and metas[1]["baseline_sha"] is None
            assert metas[2]["same_target"] is False and metas[2]["source"] == "salt-ipa"
        finally:
            _lm._PATH = old

    # --- (8) report_md surfaces lesson_downgraded ---
    from aro.report_md import render_map
    rep = render_map({
        "untried": [{"name": "f", "pct": 3.0, "symbol": "s"}],
        "tried": [], "gated": [], "not_ours": [], "generic_pct": 0.0,
        "lesson_downgraded": [
            {"fn": "f", "source": "salt", "reason": "cross-target"},
        ],
    }, "salt-msm", "probe", 1.5)
    assert "Lesson downgrades" in rep and "cross-target" in rep and "`f`" in rep

    print("case_67 OK: lesson scoping + tried-bucket freshness")
