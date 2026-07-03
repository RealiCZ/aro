"""vcs — git plumbing, with a timeout on EVERY call.

cargo and claude subprocesses always carried timeouts; git never did — a hung git
(credential prompt, lock contention, a wedged filesystem) would block the whole
harness forever, defeating every other timeout. All git invocations go through
here so that can't regress.

Thin by design: helpers return raw text / CompletedProcess-ish results and raise
RuntimeError with the stderr tail on hard failures. Policy (best-effort vs fatal)
stays with the callers.
"""
from __future__ import annotations

import shutil
import subprocess

GIT_TIMEOUT = 120        # default for quick plumbing (status/show/commit/rev-parse)
WORKTREE_TIMEOUT = 600   # worktree add on a large repo (checkout cost)


def _tail(text: str, n: int = 40) -> str:
    return "\n".join((text or "").splitlines()[-n:])


def git(cwd, *args, timeout: int = GIT_TIMEOUT):
    """Run `git -C <cwd> <args>` with a timeout. Returns the CompletedProcess
    (text mode, output captured); never raises on non-zero exit — check
    `.returncode` — but DOES raise on timeout/launch failure."""
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=timeout)


def worktree_add(repo, path, ref, *, timeout: int = WORKTREE_TIMEOUT) -> None:
    """`git worktree add --detach <path> <ref>`; raises RuntimeError on failure."""
    out = git(repo, "worktree", "add", "--detach", str(path), ref, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(_tail(out.stderr))


def worktree_remove(repo, path) -> None:
    """Best-effort worktree teardown (git unregister + rmtree the leftovers)."""
    try:
        git(repo, "worktree", "remove", "--force", str(path))
    except Exception:
        pass
    shutil.rmtree(path, ignore_errors=True)


def submodule_update(wt, *, timeout: int) -> None:
    """Populate submodules in a fresh worktree (offline, from the local object
    store). Best-effort at the call layer — a repo with none is a no-op."""
    git(wt, "submodule", "update", "--init", "--recursive", timeout=timeout)


def rev_parse(repo, ref: str):
    """Resolved sha for `ref`, or None when it doesn't resolve."""
    out = git(repo, "rev-parse", ref)
    return out.stdout.strip() if out.returncode == 0 else None


def status_porcelain(wt) -> str:
    """`git status --porcelain` text; raises RuntimeError on failure."""
    out = git(wt, "status", "--porcelain")
    if out.returncode != 0:
        raise RuntimeError(_tail(out.stderr))
    return out.stdout


def show_blob(wt, ref_path: str):
    """`git show <ref>:<path>` content, or None when the blob doesn't exist."""
    out = git(wt, "show", ref_path)
    return out.stdout if out.returncode == 0 else None


def commit_all(wt, message: str):
    """`git commit -aqm <message>` with a pinned identity (works on machines with
    no git user configured). Returns the CompletedProcess — caller checks."""
    return git(wt, "-c", "user.name=aro", "-c", "user.email=aro@example.invalid",
               "commit", "-aqm", message)
