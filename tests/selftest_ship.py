"""T39: ship baseline gate — stamp baseline_sha, ship gate, sweep preflight.

Hermetic: tmp git repos (local bare origin for fetch paths). No cargo, no network.
"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

from aro import manifest as manifestmod
from aro import reverify as reverify_mod
from aro import ship as shipmod
from aro import spec as specmod
from aro import sweep as sweepmod
from aro import terminal as terminalmod
from aro import vcs
from aro.events import EventLog


def _git(repo, *args, check=True, timeout=60):
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={r.returncode}): {r.stderr}")
    return r


def _commit(repo, msg: str):
    return _git(repo, "-c", "user.name=aro", "-c", "user.email=aro@example.invalid",
                "commit", "-qm", msg)


def _init_repo(path: Path, *, first_msg="init") -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "checkout", "-q", "-b", "main")
    (path / "README").write_text("v1\n")
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "lib.rs").write_text("fn f() {}\n")
    _git(path, "add", ".")
    _commit(path, first_msg)
    return vcs.rev_parse(path, "HEAD")


def _advance(repo: Path, *, path="src/lib.rs", content=None, msg="advance") -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if content is not None else f"// {msg}\nfn f() {{}}\n")
    _git(repo, "add", ".")
    _commit(repo, msg)
    return vcs.rev_parse(repo, "HEAD")


def _mini_spec(repo: Path, *, baseline_ref="HEAD", name="ship-t39",
               regions=None, ship_target=None):
    d = {
        "name": name,
        "target_repo": {"path": str(repo), "baseline_ref": baseline_ref},
        "metric": "ns",
        "hot_path": {"file": "src/lib.rs", "fn": "f"},
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "run": {"generator": "agentic", "stop": {"max_rounds": 1, "dry_rounds": 1},
                "aa_runs": 1, "ab_pairs": 1},
        "constraints": {"editable": regions or ["src/lib.rs"]},
    }
    if ship_target is not None:
        d["ship_target"] = ship_target
    return specmod.from_dict(d)


def _mergeable_entry(order, baseline_sha, *, sha256="abc", verdict="TERMINAL_CONFIRMED"):
    stamp = {
        "verdict": verdict,
        "source": "terminal.json",
        "sha256": sha256,
    }
    if baseline_sha is not None:
        stamp["baseline_sha"] = baseline_sha
    return {
        "order": order,
        "id": f"c{order}",
        "fn": "f",
        "mergeable": True,
        "regime": "byte-identical",
        "critic_verdict": "pass",
        "delta_pct": -1.0,
        "terminal": verdict,
        "terminal_stamp": stamp,
    }


def _write_manifest(path: Path, entries, *, baseline_ref="HEAD"):
    doc = {
        "spec": "ship-t39",
        "baseline_ref": baseline_ref,
        "accepted": entries,
    }
    path.write_text(json.dumps(doc, indent=1) + "\n")
    return doc


def case_54():
    """T39: ship baseline gate + stamp baseline_sha + sweep preflight."""
    print("=== case 54: ship baseline gate (T39) ===")

    # --- (a) terminal_doc_dict records baseline_sha; rejudge preserves it -----
    class _R:
        def to_dict(self):
            return {
                "verdict": "TERMINAL_CONFIRMED",
                "bench_ir_rows": {"r": -1.0},
                "profile_fingerprint": "fp-xyz",
                "epsilon_pct": 0.1,
                "rounds": 3,
                "floors_source": "default",
                "notes": [],
                "rows": [{
                    "row_key": "r", "base_ir": 1000, "cand_ir": 990,
                    "delta_pct": -1.0, "status": "improved", "floor_pct": 0.5,
                }],
            }

    doc = terminalmod.terminal_doc_dict(
        _R(), measured_orders={1, 2}, baseline_sha="a" * 40)
    assert doc["baseline_sha"] == "a" * 40, doc
    assert doc["measured_orders"] == [1, 2]
    # omit when unavailable
    doc_no = terminalmod.terminal_doc_dict(_R())
    assert "baseline_sha" not in doc_no

    # rejudge preserves baseline_sha from input evidence
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        in_doc = {
            "verdict": "TERMINAL_CONFIRMED",
            "bench_ir_rows": {"r": -1.0},
            "profile_fingerprint": "fp-xyz",
            "epsilon_pct": 0.1,
            "rounds": 1,
            "floors_source": "default",
            "notes": [],
            "baseline_sha": "b" * 40,
            "rows": [{
                "row_key": "r", "base_ir": 1000, "cand_ir": 990,
                "delta_pct": -1.0, "status": "improved", "floor_pct": 0.5,
            }],
        }
        # Offline rejudge helper: preserve path used by CLI
        measured = terminalmod._effective_measured_orders(in_doc, None)
        prior = in_doc.get("baseline_sha")
        # Build a TerminalResult via rejudge_terminal_doc
        judged = terminalmod.rejudge_terminal_doc(
            in_doc, epsilon_pct=0.1, default_floor_pct=1.0,
            control_lanes=[], protected_row_families=None)
        out = terminalmod.terminal_doc_dict(
            judged, measured_orders=measured, baseline_sha=prior)
        assert out["baseline_sha"] == "b" * 40, out
    print("#54a OK: terminal_doc_dict + rejudge preserve baseline_sha")

    # --- (b) apply_terminal copies baseline_sha into terminal_stamp ------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        term_path = td / "terminal.json"
        term_doc = {
            "verdict": "TERMINAL_CONFIRMED",
            "bench_ir_rows": {"r": -2.0},
            "profile_fingerprint": "fp",
            "epsilon_pct": 0.1,
            "rounds": 1,
            "floors_source": "default",
            "notes": [],
            "baseline_sha": "c" * 40,
            "rows": [{
                "row_key": "r", "base_ir": 1000, "cand_ir": 980,
                "delta_pct": -2.0, "status": "improved", "floor_pct": 0.5,
            }],
        }
        term_path.write_text(json.dumps(term_doc) + "\n")
        man = {
            "accepted": [{
                "order": 1, "id": "x", "fn": "f",
                "regime": "byte-identical", "critic_verdict": "pass",
                "delta_pct": -1.0, "mergeable": False,
            }],
        }
        man = manifestmod.apply_terminal(
            man, term_doc, terminal_required=True,
            source=str(term_path), control_lanes=[])
        stamp = man["accepted"][0]["terminal_stamp"]
        assert stamp["baseline_sha"] == "c" * 40, stamp
        assert stamp["verdict"] == "TERMINAL_CONFIRMED"
        assert "sha256" in stamp and stamp["source"]
        # build_terminal_stamp_from_source also carries it
        s2 = manifestmod.build_terminal_stamp_from_source(
            str(term_path), control_lanes=[])
        assert s2["baseline_sha"] == "c" * 40
        # legacy doc without baseline_sha still stamps (no field)
        term_legacy = dict(term_doc)
        del term_legacy["baseline_sha"]
        leg_path = td / "legacy.json"
        leg_path.write_text(json.dumps(term_legacy) + "\n")
        s3 = manifestmod.build_terminal_stamp_from_source(
            str(leg_path), control_lanes=[])
        assert "baseline_sha" not in s3
    print("#54b OK: apply_terminal copies baseline_sha into stamp")

    # --- (c) ship gate PASS / FAIL / missing / mixed / no-fetch / fetch fail --
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # Local clone + bare origin so fetch works offline.
        origin = td / "origin.git"
        work = td / "work"
        sha1 = _init_repo(work)
        _git(td, "clone", "--bare", "-q", str(work), str(origin))
        # Point origin remote at bare; keep work as the "target repo".
        _git(work, "remote", "remove", "origin", check=False)
        _git(work, "remote", "add", "origin", str(origin))
        # Ensure origin/main tracks the bare tip.
        _git(work, "fetch", "origin", "main")

        man_path = td / "manifest.json"
        _write_manifest(man_path, [_mergeable_entry(1, sha1, sha256="deadbeef")])
        sp = _mini_spec(work, baseline_ref=sha1)

        # PASS with fetch (local bare origin)
        buf = io.StringIO()
        code = shipmod.gate(sp, man_path, target="origin/main", file=buf)
        assert code == 0, buf.getvalue()
        assert "PASS" in buf.getvalue()
        assert sha1 in buf.getvalue()

        # PASS with --no-fetch
        buf = io.StringIO()
        code = shipmod.gate(
            sp, man_path, target="origin/main", no_fetch=True, file=buf)
        assert code == 0, buf.getvalue()

        # FAIL when head advances on origin
        _advance(work, msg="move-main")
        # Push advance into bare origin
        _git(work, "push", "origin", "main")
        # Manifest still certified on sha1
        buf = io.StringIO()
        code = shipmod.gate(sp, man_path, target="origin/main", file=buf)
        assert code == 1, buf.getvalue()
        assert "FAIL" in buf.getvalue()
        assert "re-certification" in buf.getvalue().lower() or \
            "re-certification required" in buf.getvalue()

        # FAIL on missing baseline_sha (legacy stamp)
        _write_manifest(man_path, [_mergeable_entry(1, None)])
        # Reset origin to sha1 so head matches would-be; still fail closed
        _git(work, "update-ref", "refs/heads/main", sha1)
        _git(work, "push", "--force", "origin", "main")
        buf = io.StringIO()
        code = shipmod.gate(sp, man_path, target="origin/main", file=buf)
        assert code == 1, buf.getvalue()
        assert "predates baseline recording" in buf.getvalue()

        # FAIL on mixed stamps
        _write_manifest(man_path, [
            _mergeable_entry(1, sha1, sha256="aa"),
            _mergeable_entry(2, "f" * 40, sha256="bb"),
        ])
        buf = io.StringIO()
        code = shipmod.gate(sp, man_path, target="origin/main", file=buf)
        assert code == 1, buf.getvalue()
        assert "mixed" in buf.getvalue().lower()

        # FAIL nothing to ship
        _write_manifest(man_path, [{
            "order": 1, "id": "x", "mergeable": False,
            "terminal_stamp": {"verdict": "TERMINAL_UNTOUCHED",
                               "source": "t", "sha256": "x",
                               "baseline_sha": sha1},
        }])
        buf = io.StringIO()
        code = shipmod.gate(sp, man_path, target="origin/main", file=buf)
        assert code == 1
        assert "nothing to ship" in buf.getvalue()

        # fetch-failure → exit 1 (point origin at a missing bare)
        _write_manifest(man_path, [_mergeable_entry(1, sha1)])
        _git(work, "remote", "set-url", "origin", str(td / "missing.git"))
        err = io.StringIO()
        with redirect_stderr(err):
            code = shipmod.gate(sp, man_path, target="origin/main", file=io.StringIO())
        assert code == 1
        assert "fetch" in err.getvalue().lower() or "ERROR" in err.getvalue()
    print("#54c OK: ship gate PASS/FAIL/missing/mixed/no-fetch/fetch-fail")

    # --- (d) sweep baseline preflight ----------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        repo = td / "repo"
        sha0 = _init_repo(repo)
        # Pin at sha0; advance with region churn
        _advance(repo, path="src/lib.rs", content="fn f() { /* churn */ }\n",
                 msg="region-churn")
        sp = _mini_spec(repo, baseline_ref=sha0, regions=["src/lib.rs"])
        events_path = td / "events.jsonl"
        ev = EventLog(events_path, also_console=False)

        try:
            sweepmod._preflight_baseline(sp, ev, allow_stale=False)
            raise AssertionError("expected SystemExit on region churn")
        except SystemExit as se:
            assert se.code == 1
        lines = [json.loads(x) for x in events_path.read_text().splitlines() if x]
        assert any(e.get("event") == "baseline_preflight"
                   and e.get("status") == "fail" for e in lines), lines

        # allow-stale override → warn, no exit
        events_path.write_text("")
        ev2 = EventLog(events_path, also_console=False)
        sweepmod._preflight_baseline(sp, ev2, allow_stale=True)
        lines = [json.loads(x) for x in events_path.read_text().splitlines() if x]
        assert any(e.get("event") == "baseline_preflight"
                   and e.get("status") == "warn"
                   and e.get("allow_stale") is True for e in lines), lines

        # out-of-region churn only → warn, continue
        repo2 = td / "repo2"
        sha_a = _init_repo(repo2)
        _advance(repo2, path="Cargo.toml", content="[package]\nname='x'\n",
                 msg="dep-churn")
        sp2 = _mini_spec(repo2, baseline_ref=sha_a, regions=["src/lib.rs"])
        events_path.write_text("")
        ev3 = EventLog(events_path, also_console=False)
        sweepmod._preflight_baseline(sp2, ev3, allow_stale=False)
        lines = [json.loads(x) for x in events_path.read_text().splitlines() if x]
        assert any(e.get("event") == "baseline_preflight"
                   and e.get("status") == "warn" for e in lines), lines
        assert any(e.get("verdict") == "still-current" for e in lines), lines

        # current → pass
        repo3 = td / "repo3"
        sha_c = _init_repo(repo3)
        sp3 = _mini_spec(repo3, baseline_ref=sha_c)
        events_path.write_text("")
        ev4 = EventLog(events_path, also_console=False)
        sweepmod._preflight_baseline(sp3, ev4)
        lines = [json.loads(x) for x in events_path.read_text().splitlines() if x]
        assert any(e.get("event") == "baseline_preflight"
                   and e.get("status") == "pass" for e in lines), lines
    print("#54d OK: sweep baseline preflight fail/warn/pass/override")

    # --- (e) recheck candidates --baseline records effective baseline_sha ----
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        repo = td / "repo"
        sha0 = _init_repo(repo)
        sha1 = _advance(repo, msg="next")
        run = td / "run"
        run.mkdir()
        # Minimal single-entry manifest with a patch
        patch_dir = run / "a1" / "patches"
        patch_dir.mkdir(parents=True)
        (patch_dir / "c1.txt").write_text(
            "<<<<<<< SEARCH\nfn f() {}\n=======\nfn f() { 1; }\n>>>>>>> REPLACE\n")
        man = {
            "spec": "ship-t39",
            "baseline_ref": sha0,
            "accepted": [{
                "order": 1, "id": "c1", "fn": "f", "attempt": "a1",
                "mergeable": True, "regime": "byte-identical",
                "patch_path": "a1/patches/c1.txt",
                "acceptance_seq": 0, "parent": sha0,
            }],
        }
        (run / "manifest.json").write_text(json.dumps(man) + "\n")

        class _T:
            name = "rv"
            differential_required = False
            has_differential = False
            baseline_sha = sha0

            def __init__(self):
                self._owned = []

            def make_worktree(self, tag):
                d = Path(tempfile.mkdtemp(prefix=f"rv-{tag}-"))
                (d / "src").mkdir()
                (d / "src" / "lib.rs").write_text("fn f() {}\n")
                self._owned.append(d)
                return d

            def remove_worktree(self, work):
                import shutil
                shutil.rmtree(work, ignore_errors=True)

            def apply(self, patch, work):
                for e in patch.edits:
                    f = Path(work) / e.path
                    content = f.read_text()
                    if e.search not in content:
                        raise RuntimeError("search not found")
                    f.write_text(content.replace(e.search, e.replace, 1))

            def build(self, work):
                return "ok"

            def test(self, work):
                return 1

            def differential(self, work, baseline):
                return True

        sp = _mini_spec(repo, baseline_ref=sha0)
        target = _T()
        # Override path: resolve sha1 in repo and stamp onto target
        doc = reverify_mod.reverify(
            sp, run, target=target, baseline_override=sha1, n_pre=1)
        assert doc.get("baseline_sha") == sha1, doc
        assert doc.get("baseline_override") == sha1, doc
        # Without override, records target's baseline_sha
        target2 = _T()
        target2.baseline_sha = sha0
        doc2 = reverify_mod.reverify(sp, run, target=target2, n_pre=1)
        assert doc2.get("baseline_sha") == sha0, doc2
    print("#54e OK: recheck candidates --baseline records baseline_sha")

    # --- (f) worktree_add invalid-reference hint -----------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        repo = td / "repo"
        _init_repo(repo)
        missing = "0" * 40
        wt = td / "wt"
        try:
            vcs.worktree_add(repo, wt, missing)
            raise AssertionError("expected RuntimeError for invalid ref")
        except RuntimeError as e:
            msg = str(e).lower()
            assert "hint" in msg or "baseline" in msg, msg
            assert "fetch" in msg or "baseline_ref" in msg, msg
    print("#54f OK: worktree_add invalid-reference hint")

    # --- (g) CLI surface: ship gate + recheck --baseline + sweep flag --------
    from aro.cli import build_parser
    p = build_parser()
    a = p.parse_args([
        "ship", "gate", "targets/x.json",
        "--manifest", ".aro-runs/r", "--target", "origin/main", "--no-fetch",
    ])
    assert a.cmd == "ship" and a.ship_action == "gate"
    assert a.manifest == ".aro-runs/r" and a.target == "origin/main"
    assert a.no_fetch is True
    a2 = p.parse_args([
        "recheck", "candidates", "--spec", "t.json", "--out", "/tmp/x",
        "--baseline", "abc123",
    ])
    assert a2.baseline == "abc123"
    a3 = p.parse_args([
        "sweep", "t.json", "--attempt", "--allow-stale-baseline",
    ])
    assert a3.allow_stale_baseline is True
    print("#54g OK: CLI surface (ship gate / --baseline / --allow-stale-baseline)")

    # --- (h) resolve helpers -------------------------------------------------
    assert shipmod.resolve_ship_target(
        SimpleNamespace(raw={}, ship_target=None), None) == "origin/main"
    assert shipmod.resolve_ship_target(
        SimpleNamespace(raw={"ship_target": "upstream/dev"}, ship_target=None),
        None) == "upstream/dev"
    assert shipmod.resolve_ship_target(
        SimpleNamespace(raw={}, ship_target=None), "origin/rel/1.0") == "origin/rel/1.0"
    assert shipmod.split_remote_branch("origin/main") == ("origin", "main")
    assert shipmod.split_remote_branch("origin/rel/1.0") == ("origin", "rel/1.0")
    print("#54h OK: ship target resolution helpers")

    print("case 54 passed")
