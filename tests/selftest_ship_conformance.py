"""T40: ship conformance — machine record of PR-branch quality checks.

Hermetic: tmp git repos; checks are trivial shell (`true` / `false` / `echo` /
`sleep`). No cargo, no network. case_54 (T39 gate) stays in selftest_ship.py.
"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path

from aro import ship as shipmod
from aro import spec as specmod
from aro import vcs


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
    _git(path, "add", ".")
    _commit(path, first_msg)
    return vcs.rev_parse(path, "HEAD")


def _spec(repo: Path, *, conformance=None, name="ship-t40"):
    d = {
        "name": name,
        "target_repo": {"path": str(repo), "baseline_ref": "HEAD"},
        "metric": "ns",
        "hot_path": {"file": "src/lib.rs", "fn": "f"},
        "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "run": {"generator": "agentic", "stop": {"max_rounds": 1, "dry_rounds": 1},
                "aa_runs": 1, "ab_pairs": 1},
        "constraints": {"editable": ["src/lib.rs"]},
    }
    if conformance is not None:
        d["ship_conformance"] = conformance
    return specmod.from_dict(d)


def case_55():
    """T40: ship conformance record + fail-closed preflight + CLI surface."""
    print("=== case 55: ship conformance (T40) ===")

    # --- (a) all-green: record written, all_green true, exit 0, head_sha -----
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        sha = _init_repo(work)
        sp = _spec(work, conformance=[
            {"name": "ok1", "cmd": "true"},
            {"name": "ok2", "cmd": "echo hello"},
        ])
        buf = io.StringIO()
        code = shipmod.conformance(
            sp, work, spec_path="targets/t40.json", file=buf)
        assert code == 0, buf.getvalue()
        assert "PASS" in buf.getvalue()
        rec_path = work / ".aro-conformance.json"
        assert rec_path.is_file(), rec_path
        rec = json.loads(rec_path.read_text())
        assert rec["all_green"] is True, rec
        assert rec["head_sha"] == sha, rec
        assert rec["spec"] == "targets/t40.json"
        assert Path(rec["workdir"]).resolve() == work.resolve()
        assert len(rec["checks"]) == 2
        assert all(c["exit"] == 0 for c in rec["checks"]), rec["checks"]
        assert rec["checks"][0]["name"] == "ok1"
        assert rec["checks"][1]["name"] == "ok2"
        assert "duration_s" in rec["checks"][0]
        assert "tail" in rec["checks"][0]
        assert "hello" in (rec["checks"][1].get("tail") or "")
    print("#55a OK: all-green record + head_sha")

    # --- (b) one failing check: exit 1, both results written -----------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        _init_repo(work)
        sp = _spec(work, conformance=[
            {"name": "pass", "cmd": "true"},
            {"name": "fail", "cmd": "false"},
        ])
        buf = io.StringIO()
        code = shipmod.conformance(sp, work, file=buf)
        assert code == 1, buf.getvalue()
        assert "FAIL" in buf.getvalue()
        rec = json.loads((work / ".aro-conformance.json").read_text())
        assert rec["all_green"] is False
        assert len(rec["checks"]) == 2, rec
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["pass"]["exit"] == 0
        assert by_name["fail"]["exit"] != 0
        # verdict table printed on failure
        assert "pass" in buf.getvalue() and "fail" in buf.getvalue()
    print("#55b OK: one failing check — record keeps both results")

    # --- (c) missing / empty ship_conformance → exit 1, no record -----------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        _init_repo(work)
        sp_missing = _spec(work, conformance=None)
        buf = io.StringIO()
        code = shipmod.conformance(sp_missing, work, file=buf)
        assert code == 1, buf.getvalue()
        assert "no ship_conformance" in buf.getvalue()
        assert not (work / ".aro-conformance.json").exists()

        sp_empty = _spec(work, conformance=[])
        buf = io.StringIO()
        code = shipmod.conformance(sp_empty, work, file=buf)
        assert code == 1, buf.getvalue()
        assert "no ship_conformance" in buf.getvalue()
    print("#55c OK: missing/empty ship_conformance fail-closed")

    # --- (d) dirty workdir (tracked change) → exit 1 ------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        _init_repo(work)
        (work / "README").write_text("dirty\n")  # tracked file modified
        sp = _spec(work, conformance=[{"name": "ok", "cmd": "true"}])
        buf = io.StringIO()
        code = shipmod.conformance(sp, work, file=buf)
        assert code == 1, buf.getvalue()
        assert "uncommitted" in buf.getvalue().lower() or "dirty" in buf.getvalue().lower()
        assert not (work / ".aro-conformance.json").exists()

        # untracked-only is fine
        _git(work, "checkout", "--", "README")
        (work / "untracked.txt").write_text("noise\n")
        buf = io.StringIO()
        code = shipmod.conformance(sp, work, file=buf)
        assert code == 0, buf.getvalue()
        assert (work / ".aro-conformance.json").is_file()
    print("#55d OK: dirty tracked fails; untracked alone ok")

    # --- (e) --out override path respected ----------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        _init_repo(work)
        out = td / "custom" / "record.json"
        sp = _spec(work, conformance=[{"name": "ok", "cmd": "true"}])
        buf = io.StringIO()
        code = shipmod.conformance(sp, work, out_path=out, file=buf)
        assert code == 0, buf.getvalue()
        assert out.is_file(), out
        assert not (work / ".aro-conformance.json").exists()
        rec = json.loads(out.read_text())
        assert rec["all_green"] is True
    print("#55e OK: --out override path")

    # --- (f) timeout path → failure recorded (exit 124) ---------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        _init_repo(work)
        sp = _spec(work, conformance=[
            {"name": "fast", "cmd": "true"},
            {"name": "slow", "cmd": "sleep 30", "timeout_s": 0.3},
        ])
        buf = io.StringIO()
        code = shipmod.conformance(sp, work, file=buf)
        assert code == 1, buf.getvalue()
        rec = json.loads((work / ".aro-conformance.json").read_text())
        assert rec["all_green"] is False
        by_name = {c["name"]: c for c in rec["checks"]}
        assert by_name["fast"]["exit"] == 0
        assert by_name["slow"]["exit"] == shipmod.TIMEOUT_EXIT
        assert "TIMEOUT" in (by_name["slow"].get("tail") or "")
        # did not stop at first failure: both checks present
        assert len(rec["checks"]) == 2
    print("#55f OK: timeout recorded as failure; all checks run")

    # --- (g) CLI argparse surface -------------------------------------------
    from aro.cli import build_parser
    p = build_parser()
    a = p.parse_args([
        "ship", "conformance", "targets/x.json",
        "--workdir", "/tmp/pr-branch",
        "--out", "/tmp/conf.json",
    ])
    assert a.cmd == "ship" and a.ship_action == "conformance"
    assert a.spec == "targets/x.json"
    assert a.workdir == "/tmp/pr-branch"
    assert a.out == "/tmp/conf.json"
    # gate surface still intact
    a2 = p.parse_args([
        "ship", "gate", "targets/x.json", "--manifest", ".aro-runs/r",
    ])
    assert a2.ship_action == "gate"
    print("#55g OK: CLI surface (ship conformance + gate)")

    # --- (h) resolve_ship_conformance helper --------------------------------
    sp = _spec(Path("/tmp"), conformance=[{"name": "a", "cmd": "true"}])
    items = shipmod.resolve_ship_conformance(sp)
    assert len(items) == 1 and items[0]["name"] == "a"
    sp2 = _spec(Path("/tmp"), conformance=None)
    assert shipmod.resolve_ship_conformance(sp2) == []
    print("#55h OK: resolve_ship_conformance helper")

    print("case 55 passed")
