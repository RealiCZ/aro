"""T32: vcs.submodule_update prefers local module gitdirs (offline worktrees).

Hermetic — real git fixtures in tmp dirs, no network. Recorded remote URLs are
either invalid hosts or local file paths; offline success with an unreachable
remote proves the local-gitdir clone path was used.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

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


def _make_bare_sub(td: Path, *, marker: str = "sub content\n") -> Path:
    src = td / "sub-src"
    src.mkdir()
    (src / "file.txt").write_text(marker)
    _git(src, "init", "-q")
    _git(src, "add", ".")
    _commit(src, "sub-init")
    bare = td / "sub.git"
    _git(td, "clone", "--bare", "-q", str(src), str(bare))
    return bare


def _make_super_with_sub(
    td: Path,
    bare: Path,
    *,
    name: str = "mysub",
    path: str = "mysub",
    poison: bool = True,
) -> Path:
    """Superproject whose main checkout has `.git/modules/<name>` populated.

    When poison=True, rewrite recorded URLs to an unreachable https host so a
    network/config clone cannot succeed — offline path is the only way.
    """
    superp = td / "super"
    superp.mkdir()
    (superp / "main.txt").write_text("main\n")
    _git(superp, "init", "-q")
    _git(superp, "add", ".")
    _commit(superp, "init")
    _git(superp, "-c", "protocol.file.allow=always",
         "submodule", "add", "--name", name, str(bare), path)
    _commit(superp, "add-sub")
    assert (superp / ".git" / "modules" / name).is_dir(), name
    if poison:
        bad = "https://invalid.invalid/sub.git"
        _git(superp, "config", "-f", ".gitmodules",
             f"submodule.{name}.url", bad)
        _git(superp, "config", f"submodule.{name}.url", bad)
        _git(superp, "add", ".gitmodules")
        _commit(superp, "poison-url")
        # Do NOT run `git submodule sync` — worktrees must see the poisoned
        # recorded URL unless command-line -c overrides redirect to the module
        # gitdir.
    return superp


def _linked_worktree(superp: Path, wt: Path) -> Path:
    _git(superp, "worktree", "add", "--detach", str(wt), "HEAD")
    return wt


def case_49():
    """T32: submodule_update offline local-gitdir + fallback + name dots."""
    print("=== case 49: submodule_update offline local module gitdir ===")

    # --- (a) end-to-end offline path in a linked worktree ---------------------
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        bare = _make_bare_sub(td, marker="offline-marker\n")
        superp = _make_super_with_sub(td, bare, name="mysub", path="mysub",
                                      poison=True)
        wt = _linked_worktree(superp, td / "wt-offline")
        # Recorded URL is unreachable https; success ⇒ local module gitdir used.
        vcs.submodule_update(wt, timeout=60)
        got = (wt / "mysub" / "file.txt").read_text()
        assert got == "offline-marker\n", got
    print("#49a OK: linked worktree offline clone from local module gitdir")

    # --- (b) submodule name containing dots (prefix/suffix strip, not split) --
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        bare = _make_bare_sub(td, marker="dot-name\n")
        superp = _make_super_with_sub(
            td, bare, name="foo.bar.baz", path="vendor/sub", poison=True)
        assert (superp / ".git" / "modules" / "foo.bar.baz").is_dir()
        names = vcs._submodule_names(superp, timeout=30)
        assert names == ["foo.bar.baz"], names
        wt = _linked_worktree(superp, td / "wt-dot")
        vcs.submodule_update(wt, timeout=60)
        assert (wt / "vendor" / "sub" / "file.txt").read_text() == "dot-name\n"
    print("#49b OK: dotted submodule name resolves to modules/<name>")

    # --- (c) fallback: no local module gitdir → plain update (recorded cmds) --
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        wt = td / "wt"
        wt.mkdir()
        (wt / ".gitmodules").write_text(
            '[submodule "orphan"]\n\tpath = orphan\n'
            '\turl = https://invalid.invalid/orphan.git\n')
        # Common dir has no modules/ at all → zero URL overrides.
        common = td / "common-git"
        common.mkdir()
        calls: list[tuple] = []

        def fake_git(cwd, *args, timeout=120):
            calls.append(args)
            cp = mock.Mock()
            cp.returncode = 0
            cp.stdout = ""
            cp.stderr = ""
            if len(args) >= 3 and args[0] == "config" and args[1] == "--file":
                cp.stdout = "submodule.orphan.path orphan\n"
                return cp
            if "rev-parse" in args and "--git-common-dir" in args:
                cp.stdout = str(common) + "\n"
                return cp
            # plain (or offline) submodule update
            return cp

        with mock.patch.object(vcs, "git", side_effect=fake_git):
            vcs.submodule_update(wt, timeout=30)

        sub_updates = [a for a in calls
                       if a[-3:] == ("submodule", "update", "--init")
                       or (len(a) >= 4 and a[-4:] == (
                           "submodule", "update", "--init", "--recursive"))]
        # Exactly one plain update: no -c overrides (modules dir absent).
        assert len(sub_updates) == 1, calls
        plain = sub_updates[0]
        assert plain == ("submodule", "update", "--init", "--recursive"), plain
        assert not any(a[:1] == ("-c",) or "-c" in a for a in sub_updates)
    print("#49c OK: absent module gitdir → single plain submodule update")

    # --- (d) offline attempt fails → fall back to plain update ----------------
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        wt = td / "wt"
        wt.mkdir()
        (wt / ".gitmodules").write_text(
            '[submodule "x"]\n\tpath = x\n\turl = https://example.invalid/x.git\n')
        common = td / "common-git"
        (common / "modules" / "x").mkdir(parents=True)
        calls: list[tuple] = []

        def fake_git(cwd, *args, timeout=120):
            calls.append(args)
            cp = mock.Mock()
            cp.stderr = ""
            if len(args) >= 3 and args[0] == "config" and args[1] == "--file":
                cp.returncode = 0
                cp.stdout = "submodule.x.path x\n"
                return cp
            if "rev-parse" in args and "--git-common-dir" in args:
                cp.returncode = 0
                cp.stdout = str(common) + "\n"
                return cp
            if args[-4:] == ("submodule", "update", "--init", "--recursive"):
                # Offline path carries -c overrides; fail it once so fallback runs.
                if "-c" in args:
                    cp.returncode = 1
                    cp.stdout = ""
                    cp.stderr = "simulated offline fail"
                    return cp
                cp.returncode = 0
                cp.stdout = ""
                return cp
            cp.returncode = 0
            cp.stdout = ""
            return cp

        with mock.patch.object(vcs, "git", side_effect=fake_git):
            vcs.submodule_update(wt, timeout=30)

        updates = [a for a in calls
                   if a[-4:] == ("submodule", "update", "--init", "--recursive")]
        assert len(updates) == 2, calls
        offline, plain = updates
        assert offline[0:2] == ("-c", "protocol.file.allow=always"), offline
        assert any(
            offline[i:i + 2] == ("-c", f"submodule.x.url={common / 'modules' / 'x'}")
            for i in range(len(offline) - 1)
        ), offline
        assert plain == ("submodule", "update", "--init", "--recursive"), plain
    print("#49d OK: offline failure falls back to plain update")

    # --- (e) no .gitmodules → single plain update, no crash -------------------
    with tempfile.TemporaryDirectory() as d:
        td = Path(d)
        repo = td / "empty"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.txt").write_text("a\n")
        _git(repo, "add", ".")
        _commit(repo, "init")
        calls: list[tuple] = []
        real_git = vcs.git

        def wrapping_git(cwd, *args, timeout=120):
            calls.append(args)
            return real_git(cwd, *args, timeout=timeout)

        with mock.patch.object(vcs, "git", side_effect=wrapping_git):
            vcs.submodule_update(repo, timeout=30)
        assert calls == [("submodule", "update", "--init", "--recursive")], calls
    print("#49e OK: no submodules → single plain update")

    print("case 49 OK")
