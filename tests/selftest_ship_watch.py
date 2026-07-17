"""T36: ship watch — PR outcome → shipped stamps / pr_feedback / reattempt queue.

Hermetic: fake gh runner returns canned JSON. No network, no real gh.
"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path

from aro import ship as shipmod
from aro import spec as specmod


def _spec(name="ship-t36"):
    return specmod.from_dict({
        "name": name,
        "target_repo": {"path": "/tmp/unused", "baseline_ref": "HEAD"},
        "metric": "ns",
        "hot_path": {"file": "src/lib.rs", "fn": "f"},
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "run": {"generator": "agentic", "stop": {"max_rounds": 1, "dry_rounds": 1},
                "aa_runs": 1, "ab_pairs": 1},
        "constraints": {"editable": ["src/lib.rs"]},
    })


def _entry(order, *, mergeable=True, fn="f", files=None, **extra):
    e = {
        "order": order,
        "id": f"c{order}",
        "fn": fn,
        "mergeable": mergeable,
        "files": files if files is not None else [f"src/{fn}.rs"],
        "regime": "byte-identical",
        "critic_verdict": "pass",
        "delta_pct": -1.0,
    }
    e.update(extra)
    return e


def _write_run(td: Path, entries) -> Path:
    run = td / "run"
    run.mkdir(parents=True, exist_ok=True)
    man = {
        "spec": "ship-t36",
        "baseline_ref": "abc",
        "accepted": entries,
    }
    (run / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return run


class _FakeGh:
    """Injectable gh runner. Dispatches on argv[0:2]."""

    def __init__(self, view: dict, review_comments=None, *, fail=False,
                 fail_msg="gh failed"):
        self.view = view
        self.review_comments = (
            review_comments if review_comments is not None
            else view.get("reviewComments", []))
        self.fail = fail
        self.fail_msg = fail_msg
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail:
            return subprocess.CompletedProcess(
                args=["gh", *argv], returncode=1,
                stdout="", stderr=self.fail_msg)
        if len(argv) >= 2 and argv[0] == "pr" and argv[1] == "view":
            # Strip reviewComments from view stdout if present — production
            # gets them from the api call; tests can still pre-seed.
            payload = dict(self.view)
            # If reviewComments was only for harvest convenience and we also
            # serve api, leave them in view so a single-call test works when
            # fetch_pr_payload sees them already present.
            return subprocess.CompletedProcess(
                args=["gh", *argv], returncode=0,
                stdout=json.dumps(payload), stderr="")
        if argv and argv[0] == "api":
            return subprocess.CompletedProcess(
                args=["gh", *argv], returncode=0,
                stdout=json.dumps(self.review_comments), stderr="")
        return subprocess.CompletedProcess(
            args=["gh", *argv], returncode=1,
            stdout="", stderr=f"unexpected argv: {argv}")


def _payload_merged(**kw):
    base = {
        "state": "MERGED",
        "mergedAt": "2026-07-17T12:00:00Z",
        "mergeCommit": {"oid": "m" * 40},
        "reviews": [],
        "comments": [],
        "reviewDecision": "",
        "headRefOid": "h" * 40,
        "url": "https://github.com/org/repo/pull/42",
    }
    base.update(kw)
    return base


def _payload_closed(**kw):
    base = {
        "state": "CLOSED",
        "mergedAt": None,
        "mergeCommit": None,
        "reviews": [
            {
                "author": {"login": "rev1"},
                "body": "please fix the hot path",
                "state": "CHANGES_REQUESTED",
            },
        ],
        "comments": [
            {
                "author": {"login": "alice"},
                "body": "closing — needs rework on the algorithm",
            },
        ],
        "reviewDecision": "CHANGES_REQUESTED",
        "headRefOid": "h" * 40,
        "url": "https://github.com/org/repo/pull/99",
        "reviewComments": [
            {
                "author": {"login": "rev1"},
                "body": "this branch is wrong for cold accounts",
                "path": "src/f.rs",
                "original_position": 12,
                "original_line": 40,
                "line": 40,
            },
            {
                "author": {"login": "rev1"},
                "body": "unrelated file nit",
                "path": "docs/OTHER.md",
                "original_position": 1,
                "line": 1,
            },
        ],
    }
    base.update(kw)
    return base


def case_57():
    """T36: ship watch — merged/closed/changes-requested/open/gh-fail/CLI/docs."""
    print("=== case 57: ship watch (T36) ===")
    sp = _spec()

    # --- (a) merged: stamp mergeable only; idempotent re-run -----------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        run = _write_run(td, [
            _entry(1, mergeable=True, fn="f", files=["src/f.rs"]),
            _entry(2, mergeable=False, fn="g", files=["src/g.rs"]),
            _entry(3, mergeable=True, fn="h", files=["src/h.rs"]),
        ])
        gh = _FakeGh(_payload_merged())
        buf = io.StringIO()
        code = shipmod.watch(
            sp, run, "42", gh_runner=gh, file=buf)
        assert code == 0, buf.getvalue()
        assert "MERGED" in buf.getvalue()
        man = json.loads((run / "manifest.json").read_text())
        by_order = {a["order"]: a for a in man["accepted"]}
        assert by_order[1]["shipped"]["state"] == "merged"
        assert by_order[1]["shipped"]["merge_sha"] == "m" * 40
        assert "github.com/org/repo/pull/42" in by_order[1]["shipped"]["pr"]
        assert by_order[3]["shipped"]["state"] == "merged"
        assert "shipped" not in by_order[2], by_order[2]
        stamp1 = json.dumps(by_order[1]["shipped"], sort_keys=True)

        # idempotent re-run: no duplicate/altered stamps
        gh2 = _FakeGh(_payload_merged())
        buf2 = io.StringIO()
        code = shipmod.watch(sp, run, "42", gh_runner=gh2, file=buf2)
        assert code == 0
        man2 = json.loads((run / "manifest.json").read_text())
        by2 = {a["order"]: a for a in man2["accepted"]}
        assert json.dumps(by2[1]["shipped"], sort_keys=True) == stamp1
        assert "shipped" not in by2[2]
        # no pr_feedback / queue on merge
        assert not (run / "pr_feedback").exists()
        assert not (run / "reattempt-queue.json").exists()
    print("#57a OK: merged stamps mergeable only; idempotent; non-mergeable untouched")

    # --- (b) closed: feedback bound by path; unbound null; queue + dedup -----
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        run = _write_run(td, [
            _entry(1, mergeable=True, fn="f", files=["src/f.rs"]),
            _entry(2, mergeable=True, fn="g", files=["src/g.rs"]),
        ])
        man_before = (run / "manifest.json").read_bytes()
        gh = _FakeGh(_payload_closed())
        buf = io.StringIO()
        code = shipmod.watch(sp, run, "99", gh_runner=gh, file=buf)
        assert code == 0, buf.getvalue()
        assert "CLOSED" in buf.getvalue()
        # manifest untouched
        assert (run / "manifest.json").read_bytes() == man_before

        fb = run / "pr_feedback" / "99.json"
        assert fb.is_file(), fb
        doc = json.loads(fb.read_text())
        assert doc["verdict"] == "closed"
        assert doc["pr_key"] == "99"
        items = doc["items"]
        # inline bound to order 1 via src/f.rs
        inline_bound = [
            it for it in items
            if it.get("kind") == "review_comment" and it.get("path") == "src/f.rs"
        ]
        assert len(inline_bound) == 1, items
        assert inline_bound[0]["entry"] == {"order": 1, "fn": "f"}
        assert "cold accounts" in inline_bound[0]["body"]
        # unbound path
        inline_unbound = [
            it for it in items
            if it.get("kind") == "review_comment" and it.get("path") == "docs/OTHER.md"
        ]
        assert len(inline_unbound) == 1
        assert inline_unbound[0]["entry"] is None
        # top-level review + comment present with entry null
        kinds = {it["kind"] for it in items}
        assert "review" in kinds and "comment" in kinds

        qpath = run / "reattempt-queue.json"
        assert qpath.is_file()
        queue = json.loads(qpath.read_text())
        assert isinstance(queue, list) and len(queue) >= 1
        # only bound entry seeded
        assert all(s["order"] == 1 for s in queue), queue
        assert all(s["status"] == "pending" for s in queue)
        assert all("hint" in s and "pr" in s for s in queue)
        n_first = len(queue)

        # second run: feedback overwritten; queue dedups
        gh3 = _FakeGh(_payload_closed())
        code = shipmod.watch(sp, run, "99", gh_runner=gh3, file=io.StringIO())
        assert code == 0
        queue2 = json.loads(qpath.read_text())
        assert len(queue2) == n_first, (n_first, queue2)
        # feedback file still single overwrite
        assert fb.is_file()
        doc2 = json.loads(fb.read_text())
        assert doc2["verdict"] == "closed"
    print("#57b OK: closed harvest + path bind + unbound null + queue dedup")

    # --- (c) changes-requested: harvest + queue; manifest byte-identical -----
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        run = _write_run(td, [
            _entry(1, mergeable=True, fn="f", files=["src/f.rs"]),
        ])
        man_before = (run / "manifest.json").read_bytes()
        payload = _payload_closed(
            state="OPEN",
            mergedAt=None,
            mergeCommit=None,
            reviewDecision="CHANGES_REQUESTED",
            url="https://github.com/org/repo/pull/7",
        )
        gh = _FakeGh(payload)
        buf = io.StringIO()
        code = shipmod.watch(sp, run, "7", gh_runner=gh, file=buf)
        assert code == 0, buf.getvalue()
        assert "CHANGES_REQUESTED" in buf.getvalue()
        assert "re-certified" in buf.getvalue().lower() or "stays open" in buf.getvalue().lower()
        assert (run / "manifest.json").read_bytes() == man_before
        assert (run / "pr_feedback" / "7.json").is_file()
        assert (run / "reattempt-queue.json").is_file()
        # no shipped stamps
        man = json.loads((run / "manifest.json").read_text())
        assert all("shipped" not in a for a in man["accepted"])
    print("#57c OK: changes-requested harvest; manifest byte-identical")

    # --- (d) open / no feedback: no-op exit 0, no files written --------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        run = _write_run(td, [_entry(1)])
        man_before = (run / "manifest.json").read_bytes()
        payload = {
            "state": "OPEN",
            "mergedAt": None,
            "mergeCommit": None,
            "reviews": [],
            "comments": [],
            "reviewDecision": "REVIEW_REQUIRED",
            "headRefOid": "h" * 40,
            "url": "https://github.com/org/repo/pull/3",
        }
        gh = _FakeGh(payload, review_comments=[])
        buf = io.StringIO()
        code = shipmod.watch(sp, run, "3", gh_runner=gh, file=buf)
        assert code == 0, buf.getvalue()
        assert "OPEN" in buf.getvalue() and "no-op" in buf.getvalue().lower()
        assert (run / "manifest.json").read_bytes() == man_before
        assert not (run / "pr_feedback").exists()
        assert not (run / "reattempt-queue.json").exists()
    print("#57d OK: open/no-feedback no-op")

    # --- (e) gh failure → exit 1 --------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        run = _write_run(td, [_entry(1)])
        gh = _FakeGh({}, fail=True, fail_msg="HTTP 401")
        err = io.StringIO()
        # watch prints errors to stderr
        import sys
        old_err = sys.stderr
        sys.stderr = err
        try:
            code = shipmod.watch(
                sp, run, "1", gh_runner=gh, file=io.StringIO())
        finally:
            sys.stderr = old_err
        assert code == 1
        assert "ERROR" in err.getvalue() or "failed" in err.getvalue().lower()
        assert not (run / "pr_feedback").exists()
    print("#57e OK: gh failure exit 1")

    # --- (f) CLI argparse surface -------------------------------------------
    from aro.cli import build_parser
    p = build_parser()
    a = p.parse_args([
        "ship", "watch", "targets/x.json",
        "--manifest", ".aro-runs/r",
        "--pr", "https://github.com/org/repo/pull/42",
    ])
    assert a.cmd == "ship" and a.ship_action == "watch"
    assert a.spec == "targets/x.json"
    assert a.manifest == ".aro-runs/r"
    assert "pull/42" in a.pr
    # gate + conformance still intact
    a2 = p.parse_args([
        "ship", "gate", "targets/x.json", "--manifest", ".aro-runs/r",
    ])
    assert a2.ship_action == "gate"
    a3 = p.parse_args([
        "ship", "conformance", "targets/x.json",
        "--workdir", "/tmp/pr",
    ])
    assert a3.ship_action == "conformance"
    print("#57f OK: CLI surface (watch + gate + conformance)")

    # --- (g) docs greps: byte-frozen hard rule + re-certified path -----------
    root = Path(__file__).resolve().parents[1]
    run_to_pr = (root / "skill" / "references" / "run-to-pr.md").read_text()
    assert "byte-frozen" in run_to_pr
    assert "re-certified" in run_to_pr
    assert "aro ship watch" in run_to_pr
    assert "After the PR exists" in run_to_pr
    ops = (root / "docs" / "OPERATIONS.md").read_text()
    assert "ship watch" in ops
    assert "pr_feedback" in ops
    assert "reattempt-queue.json" in ops
    assert "shipped" in ops
    print("#57g OK: docs greps (run-to-pr hard rule + OPERATIONS pr-watch)")

    # --- (h) helpers: classify / bind / pr key / hint hash -------------------
    assert shipmod.classify_pr_verdict(_payload_merged()) == "merged"
    assert shipmod.classify_pr_verdict(_payload_closed()) == "closed"
    assert shipmod.classify_pr_verdict({
        "state": "OPEN", "reviewDecision": "CHANGES_REQUESTED",
    }) == "changes_requested"
    assert shipmod.classify_pr_verdict({
        "state": "OPEN", "reviewDecision": "APPROVED",
    }) == "open"
    # CLOSED + merge evidence → merged
    assert shipmod.classify_pr_verdict({
        "state": "CLOSED",
        "mergedAt": "2026-01-01T00:00:00Z",
        "mergeCommit": {"oid": "abc"},
    }) == "merged"
    assert shipmod.pr_feedback_key("42") == "42"
    assert shipmod.pr_feedback_key(
        "https://github.com/o/r/pull/7") == "7"
    assert len(shipmod.hint_hash("hello")) == 16
    man = {"accepted": [
        _entry(1, files=["crates/x/src/lib.rs"]),
        _entry(2, files=["other.rs"]),
    ]}
    bound = shipmod.bind_entry_for_path(man, "crates/x/src/lib.rs")
    assert bound == {"order": 1, "fn": "f"}
    assert shipmod.bind_entry_for_path(man, "nope.rs") is None
    print("#57h OK: classify / bind / keys helpers")

    print("case 57 passed")
