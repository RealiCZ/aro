"""T42: ship package + ship open — certified branch, PR body, machine-gated open.

Hermetic: tmp git repos (local bare as origin), real small SEARCH/REPLACE patches,
fake gh/push runners. No network, no real gh.
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
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "lib.rs").write_text("fn f() {}\nfn g() {}\n")
    _git(path, "add", ".")
    _commit(path, first_msg)
    return vcs.rev_parse(path, "HEAD")


def _setup_origin(td: Path, work: Path) -> Path:
    """Bare origin + remote; fetch so origin/main resolves. Returns bare path."""
    origin = td / "origin.git"
    _git(td, "clone", "--bare", "-q", str(work), str(origin))
    _git(work, "remote", "remove", "origin", check=False)
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "fetch", "origin", "main")
    return origin


def _patch_text(path: str, search: str, replace: str) -> str:
    return (
        f"--- edit 1 ---\n"
        f"path: {path}\n"
        f"<<<<<<< SEARCH\n"
        f"{search}\n"
        f"=======\n"
        f"{replace}\n"
        f">>>>>>> REPLACE\n"
    )


def _spec(repo: Path, *, name="ship-t42", ship_target="origin/main",
          control_lanes=None, pr_labels=None, ship_remote=None):
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
        "ship_target": ship_target,
        "control_lanes": control_lanes if control_lanes is not None else ["ctrl_lane"],
    }
    if pr_labels is not None:
        d["pr_labels"] = pr_labels
    if ship_remote is not None:
        d["ship_remote"] = ship_remote
    return specmod.from_dict(d)


def _write_run(td: Path, sha: str, *, entries=None, terminal_doc=None,
               run_name="run42") -> Path:
    """Write a run dir with patches + manifest + optional terminal.json."""
    run = td / run_name
    run.mkdir(parents=True, exist_ok=True)
    patches = run / "a1" / "patches"
    patches.mkdir(parents=True, exist_ok=True)

    # Default: one mergeable edit on f(), optional second on g()
    (patches / "c1.txt").write_text(
        _patch_text("src/lib.rs", "fn f() {}", "fn f() { /* win */ }"))
    (patches / "c2.txt").write_text(
        _patch_text("src/lib.rs", "fn g() {}", "fn g() { /* win2 */ }"))

    term_path = run / "terminal.json"
    if terminal_doc is None:
        terminal_doc = {
            "verdict": "TERMINAL_CONFIRMED_WITH_TRADE",
            "bench_ir_rows": {
                "hot_row": -3.5,
                "other_row": 0.8,
                "ctrl_lane": 0.05,
            },
            "profile_fingerprint": "fp-test",
            "env_fingerprint": "codspeed=1;valgrind=1",
            "epsilon_pct": 0.1,
            "rounds": 3,
            "floors_source": "default",
            "baseline_sha": sha,
            "measured_orders": [1, 2],
            "notes": [
                "traded: other_row +0.8000% (cap 1.5%)",
            ],
            "rows": [
                {"row_key": "hot_row", "base_ir": 1000, "cand_ir": 965,
                 "delta_pct": -3.5, "status": "improved", "floor_pct": 0.5},
                {"row_key": "other_row", "base_ir": 1000, "cand_ir": 1008,
                 "delta_pct": 0.8, "status": "regressed", "floor_pct": 0.5},
                {"row_key": "ctrl_lane", "base_ir": 1000, "cand_ir": 1000,
                 "delta_pct": 0.05, "status": "control-ok", "floor_pct": 2.0},
            ],
        }
    term_path.write_text(json.dumps(terminal_doc, indent=2) + "\n")
    stamp_sha = __import__("hashlib").sha256(
        term_path.read_bytes()).hexdigest()

    if entries is None:
        entries = [
            {
                "order": 1, "id": "c1", "fn": "f", "attempt": "a1",
                "mergeable": True, "regime": "byte-identical",
                "critic_verdict": "pass", "delta_pct": -3.5,
                "patch_path": "a1/patches/c1.txt",
                "files": ["src/lib.rs"],
                "terminal": terminal_doc["verdict"],
                "bench_ir_rows": dict(terminal_doc["bench_ir_rows"]),
                "profile_fingerprint": "fp-test",
                "terminal_stamp": {
                    "verdict": terminal_doc["verdict"],
                    "source": str(term_path),
                    "sha256": stamp_sha,
                    "baseline_sha": sha,
                },
                "quarantine_disclosure": "required",
                "quarantine_cleared_by": "human-audit",
                "quarantine_audit": {
                    "cleared": True, "by": "alice",
                    "date": "2026-07-17",
                    "evidence": "manual review of outlier",
                    "delta_pct": -3.5,
                },
            },
            {
                "order": 2, "id": "c2", "fn": "g", "attempt": "a1",
                "mergeable": True, "regime": "byte-identical",
                "critic_verdict": "pass", "delta_pct": -1.0,
                "patch_path": "a1/patches/c2.txt",
                "files": ["src/lib.rs"],
                "terminal": terminal_doc["verdict"],
                "bench_ir_rows": dict(terminal_doc["bench_ir_rows"]),
                "profile_fingerprint": "fp-test",
                "terminal_stamp": {
                    "verdict": terminal_doc["verdict"],
                    "source": str(term_path),
                    "sha256": stamp_sha,
                    "baseline_sha": sha,
                },
                "quarantine_disclosure": "required",
                "quarantine_cleared_by": "auto-evidence",
                "reverify": {"verdict": "reverify-pass"},
            },
        ]

    man = {
        "spec": "ship-t42",
        "baseline_ref": sha,
        "accepted": entries,
        "terminal": {
            "verdict": terminal_doc["verdict"],
            "bench_ir_rows": dict(terminal_doc["bench_ir_rows"]),
            "profile_fingerprint": "fp-test",
            "terminal_stamp": {
                "verdict": terminal_doc["verdict"],
                "source": str(term_path),
                "sha256": stamp_sha,
                "baseline_sha": sha,
            },
        },
    }
    (run / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return run


class _FakePush:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, cwd, argv):
        self.calls.append((Path(cwd), list(argv)))
        if self.fail:
            return subprocess.CompletedProcess(
                args=["git", *argv], returncode=1,
                stdout="", stderr="push denied")
        return subprocess.CompletedProcess(
            args=["git", *argv], returncode=0, stdout="ok\n", stderr="")


class _FakeGh:
    def __init__(self, *, url="https://github.com/org/repo/pull/99", fail=False):
        self.url = url
        self.fail = fail
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail:
            return subprocess.CompletedProcess(
                args=["gh", *argv], returncode=1,
                stdout="", stderr="gh failed")
        return subprocess.CompletedProcess(
            args=["gh", *argv], returncode=0,
            stdout=self.url + "\n", stderr="")


def _write_conformance(workdir: Path, *, all_green=True, head_sha=None):
    sha = head_sha or vcs.rev_parse(workdir, "HEAD")
    rec = {
        "head_sha": sha,
        "workdir": str(workdir),
        "spec": "t42",
        "checks": [{"name": "ok", "cmd": "true", "exit": 0,
                    "duration_s": 0.01, "tail": ""}],
        "all_green": all_green,
    }
    p = workdir / ".aro-conformance.json"
    p.write_text(json.dumps(rec, indent=2) + "\n")
    return p


def case_59():
    """T42: ship package + ship open hermetic suite."""
    print("=== case 59: ship package + open (T42) ===")

    # --- (a) package: gate-fail aborts (no worktree) --------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        sha = _init_repo(work)
        _setup_origin(td, work)
        # Manifest certified on wrong baseline → gate FAIL
        run = _write_run(td, "f" * 40, run_name="gatefail")
        # Fix entries' stamp baseline to wrong sha already done
        sp = _spec(work)
        wt = td / "should-not-exist"
        buf = io.StringIO()
        code = shipmod.package(
            sp, run, target="origin/main", no_fetch=True,
            workdir=wt, file=buf)
        assert code == 1, buf.getvalue()
        assert "ABORT" in buf.getvalue() or "FAIL" in buf.getvalue()
        assert not wt.exists(), "gate-fail must not create workdir"
    print("#59a OK: package gate-fail aborts")

    # --- (b) package happy path: one commit, diff == patches, body rules ------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        sha = _init_repo(work)
        _setup_origin(td, work)
        run = _write_run(td, sha, run_name="happy")
        sp = _spec(work, control_lanes=["ctrl_lane"], pr_labels=["perf"])
        wt = td / "wt-happy"
        buf = io.StringIO()
        code = shipmod.package(
            sp, run, target="origin/main", no_fetch=True,
            workdir=wt, branch="aro/ship-happy", file=buf)
        assert code == 0, buf.getvalue()
        assert "PASS" in buf.getvalue()
        assert wt.is_dir()
        # Exactly one commit ahead of origin/main
        log = _git(wt, "log", "--oneline", "origin/main..HEAD")
        lines = [ln for ln in log.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, lines
        assert "certified set" in lines[0]
        # Diff content matches both certified patches
        diff = _git(wt, "diff", "origin/main..HEAD")
        assert "/* win */" in diff.stdout
        assert "/* win2 */" in diff.stdout
        # Working tree content
        lib = (wt / "src" / "lib.rs").read_text()
        assert "/* win */" in lib and "/* win2 */" in lib
        # Branch name
        br = _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert br == "aro/ship-happy"
        # Body file
        body_path = run / "pr_body.md"
        assert body_path.is_file(), body_path
        body = body_path.read_text()
        assert "## Summary" in body
        assert "## Delta (Ir-first)" in body
        assert "hot_row" in body
        # control-lane row must NOT appear as a Delta table headline
        # (may appear only in the lanes note)
        delta_section = body.split("## Delta (Ir-first)")[1].split("## ")[0]
        assert "`ctrl_lane`" not in delta_section, delta_section
        assert "ctrl_lane" in body  # listed in lanes note is fine
        # traded verbatim
        assert "## Traded regressions" in body
        assert "other_row" in body
        assert "0.8000" in body or "0.8" in body
        assert "1.5" in body
        # both disclosure variants
        assert "## Outlier disclosure" in body
        assert "human-audit" in body
        assert "alice" in body
        assert "auto-evidence" in body
        assert "reverify-pass" in body
        # provenance
        assert sha in body
        man = json.loads((run / "manifest.json").read_text())
        stamp_sha = man["accepted"][0]["terminal_stamp"]["sha256"]
        assert stamp_sha in body
        assert "baseline_sha" in body
        assert "## Files changed" in body
        assert "src/lib.rs" in body
        # next steps printed
        assert "conformance" in buf.getvalue()
        assert "open" in buf.getvalue()
    print("#59b OK: package happy path + body rules")

    # --- (c) package apply-mismatch aborts naming the order -------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        sha = _init_repo(work)
        _setup_origin(td, work)
        run = _write_run(td, sha, run_name="mismatch")
        # Corrupt patch SEARCH so apply fails at order 1
        (run / "a1" / "patches" / "c1.txt").write_text(
            _patch_text("src/lib.rs", "fn f() { NOPE }", "fn f() { x }"))
        sp = _spec(work)
        wt = td / "wt-mis"
        buf = io.StringIO()
        code = shipmod.package(
            sp, run, target="origin/main", no_fetch=True,
            workdir=wt, file=buf)
        assert code == 1, buf.getvalue()
        assert "order=1" in buf.getvalue() or "order=1" in buf.getvalue().lower()
        assert "integrity" in buf.getvalue().lower() or "apply" in buf.getvalue().lower()
    print("#59c OK: package apply-mismatch names order")

    # --- (d) open refuses: missing record / all_green false / stale head ------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        sha = _init_repo(work)
        _setup_origin(td, work)
        run = _write_run(td, sha, run_name="openref")
        sp = _spec(work, pr_labels=["needs-review", "perf"])
        wt = td / "wt-open"
        buf = io.StringIO()
        assert shipmod.package(
            sp, run, target="origin/main", no_fetch=True,
            workdir=wt, file=buf) == 0, buf.getvalue()

        push = _FakePush()
        gh = _FakeGh()

        # (d1) missing record
        buf = io.StringIO()
        code = shipmod.open_pr(
            sp, run, wt, target="origin/main", no_fetch=True,
            push_runner=push, gh_runner=gh, file=buf)
        assert code == 1, buf.getvalue()
        assert "missing conformance" in buf.getvalue().lower() or \
            "conformance record" in buf.getvalue().lower()
        assert not push.calls and not gh.calls

        # (d2) all_green false
        _write_conformance(wt, all_green=False)
        buf = io.StringIO()
        code = shipmod.open_pr(
            sp, run, wt, target="origin/main", no_fetch=True,
            push_runner=push, gh_runner=gh, file=buf)
        assert code == 1, buf.getvalue()
        assert "all_green" in buf.getvalue()
        assert not push.calls

        # (d3) head_sha != HEAD (stale)
        _write_conformance(wt, all_green=True, head_sha="a" * 40)
        buf = io.StringIO()
        code = shipmod.open_pr(
            sp, run, wt, target="origin/main", no_fetch=True,
            push_runner=push, gh_runner=gh, file=buf)
        assert code == 1, buf.getvalue()
        assert "stale" in buf.getvalue().lower() or "!=" in buf.getvalue()
        assert not push.calls
    print("#59d OK: open refuses missing / all_green false / stale head")

    # --- (e) open refuses: dirty tracked tree + non-whitelisted commit --------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        sha = _init_repo(work)
        _setup_origin(td, work)
        run = _write_run(td, sha, run_name="openref2")
        sp = _spec(work)
        wt = td / "wt-open2"
        assert shipmod.package(
            sp, run, target="origin/main", no_fetch=True,
            workdir=wt, file=io.StringIO()) == 0
        _write_conformance(wt, all_green=True)

        # dirty tracked
        (wt / "src" / "lib.rs").write_text("fn f() { dirty }\n")
        push = _FakePush()
        gh = _FakeGh()
        buf = io.StringIO()
        code = shipmod.open_pr(
            sp, run, wt, target="origin/main", no_fetch=True,
            push_runner=push, gh_runner=gh, file=buf)
        assert code == 1, buf.getvalue()
        assert "uncommitted" in buf.getvalue().lower()
        assert not push.calls
        # restore
        _git(wt, "checkout", "--", "src/lib.rs")

        # non-whitelisted post-cert commit
        (wt / "README").write_text("v2-extra\n")
        _git(wt, "add", "README")
        _commit(wt, "feat: sneaky post-cert change")
        # re-bind conformance to new HEAD so we don't trip stale first
        _write_conformance(wt, all_green=True)
        buf = io.StringIO()
        code = shipmod.open_pr(
            sp, run, wt, target="origin/main", no_fetch=True,
            push_runner=push, gh_runner=gh, file=buf)
        assert code == 1, buf.getvalue()
        assert "non-whitelisted" in buf.getvalue().lower() or \
            "sneaky" in buf.getvalue()
        assert not push.calls
    print("#59e OK: open refuses dirty tree + non-whitelisted commit")

    # --- (f) open happy path: push + gh with base/labels/body-file ------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        sha = _init_repo(work)
        _setup_origin(td, work)
        run = _write_run(td, sha, run_name="openhappy")
        sp = _spec(work, pr_labels=["perf", "aro"], ship_remote="origin")
        wt = td / "wt-oh"
        assert shipmod.package(
            sp, run, target="origin/main", no_fetch=True,
            workdir=wt, branch="aro/ship-openhappy", file=io.StringIO()) == 0
        # allowed post-cert commits (pr-discipline whitelist)
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests" / "extra.rs").write_text("// cover f\n")
        _git(wt, "add", "tests/extra.rs")
        _commit(wt, "test(ours): cover f")
        readme = (wt / "README").read_text()
        (wt / "README").write_text(readme.rstrip("\n") + "\n\n")
        _git(wt, "add", "README")
        _commit(wt, "style: cargo fmt")

        _write_conformance(wt, all_green=True)
        push = _FakePush()
        gh = _FakeGh(url="https://github.com/org/repo/pull/123")
        buf = io.StringIO()
        code = shipmod.open_pr(
            sp, run, wt, target="origin/main", no_fetch=True,
            title="perf: custom title",
            push_runner=push, gh_runner=gh, file=buf)
        assert code == 0, buf.getvalue()
        assert "PASS" in buf.getvalue()
        assert "https://github.com/org/repo/pull/123" in buf.getvalue()
        # push called with -u origin branch
        assert len(push.calls) == 1
        cwd, argv = push.calls[0]
        assert Path(cwd).resolve() == wt.resolve()
        assert argv[:3] == ["push", "-u", "origin"]
        assert "aro/ship-openhappy" in argv
        # gh pr create
        assert len(gh.calls) == 1
        gargv = gh.calls[0]
        assert gargv[0:2] == ["pr", "create"]
        assert "--title" in gargv and "perf: custom title" in gargv
        assert "--body-file" in gargv
        bf = gargv[gargv.index("--body-file") + 1]
        assert bf.endswith("pr_body.md")
        assert Path(bf).is_file()
        assert "--base" in gargv and "main" in gargv
        # labels
        assert gargv.count("--label") == 2
        assert "perf" in gargv and "aro" in gargv
    print("#59f OK: open happy path push+gh base/labels/body-file")

    # --- (g) CLI argparse surface for package + open --------------------------
    from aro.cli import build_parser
    p = build_parser()
    a = p.parse_args([
        "ship", "package", "targets/x.json",
        "--manifest", ".aro-runs/r",
        "--branch", "aro/ship-x",
        "--workdir", "/tmp/wt",
        "--no-fetch",
    ])
    assert a.cmd == "ship" and a.ship_action == "package"
    assert a.spec == "targets/x.json"
    assert a.manifest == ".aro-runs/r"
    assert a.branch == "aro/ship-x"
    assert a.workdir == "/tmp/wt"
    assert a.no_fetch is True

    a2 = p.parse_args([
        "ship", "open", "targets/x.json",
        "--manifest", ".aro-runs/r",
        "--workdir", "/tmp/wt",
        "--record", "/tmp/rec.json",
        "--title", "hello",
    ])
    assert a2.ship_action == "open"
    assert a2.workdir == "/tmp/wt"
    assert a2.record == "/tmp/rec.json"
    assert a2.title == "hello"

    # gate / conformance / watch still intact
    a3 = p.parse_args([
        "ship", "gate", "targets/x.json", "--manifest", ".aro-runs/r",
    ])
    assert a3.ship_action == "gate"
    a4 = p.parse_args([
        "ship", "conformance", "targets/x.json", "--workdir", "/tmp/pr",
    ])
    assert a4.ship_action == "conformance"
    a5 = p.parse_args([
        "ship", "watch", "targets/x.json",
        "--manifest", ".aro-runs/r", "--pr", "42",
    ])
    assert a5.ship_action == "watch"
    print("#59g OK: CLI surface package/open; gate/conformance/watch intact")

    # --- (h) docs greps -------------------------------------------------------
    root = Path(__file__).resolve().parents[1]
    run_to_pr = (root / "skill" / "references" / "run-to-pr.md").read_text()
    assert "ship package" in run_to_pr
    assert "ship open" in run_to_pr
    slots = (root / "skill" / "references" / "spec-slots.md").read_text()
    assert "ship_remote" in slots
    assert "pr_labels" in slots
    ops = (root / "docs" / "OPERATIONS.md").read_text()
    assert "ship package" in ops or "package/open" in ops or "ship open" in ops
    assert "impossible" in ops.lower()
    print("#59h OK: docs greps (run-to-pr / spec-slots / OPERATIONS)")

    print("case 59 passed")
