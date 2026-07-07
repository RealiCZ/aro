"""`aro clean` — explicit, auditable cleanup of a spec's leftover artifacts.

Three kinds of debris accumulate around a campaign:
  - worktrees under `<repo-parent>/.aro-worktrees/` that a crashed run never
    tore down;
  - their per-worktree cargo target dirs under `<repo-parent>/.aro-<name>-td/`;
  - finished run out-dirs nobody looks at anymore.

DELIBERATELY a command, never a background sweep: concurrent campaigns share
target repos, and misidentifying a live worktree kills a run. The safety rules:
  - a worktree still REGISTERED with git is presumed live and kept
    (`--registered` overrides, for after-crash cleanup when nothing is running);
  - run dirs are only touched with `--runs`, and any run dir referenced by a
    permanent-ledger `events` pointer is protected — deleting it would sever
    the audit chain behind recorded verdicts;
  - everything removed is printed first (`--dry-run` prints without removing).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import permtree, vcs


def _registered_worktrees(repo: Path) -> set:
    """Absolute paths git currently knows as worktrees of `repo`."""
    out = vcs.git(repo, "worktree", "list", "--porcelain")
    paths = set()
    for ln in (out.stdout or "").splitlines():
        if ln.startswith("worktree "):
            paths.add(str(Path(ln[len("worktree "):].strip()).resolve()))
    return paths


def _ledger_protected_names() -> set:
    """Basenames of every run dir any permanent ledger points at (the `events`
    field, `<out-dir>[#aN]`). Name-based on purpose: refs may be relative to a
    different cwd than ours, and over-protecting is the safe direction."""
    names = set()
    for spec_name in permtree.ledgers():
        for rec in permtree.load(spec_name):
            ref = (rec.get("events") or "").split("#", 1)[0].strip()
            if ref:
                names.add(Path(ref).name)
    return names


def scan(repo: Path, spec_name: str, *, runs_dir: Path = None,
         registered: bool = False) -> dict:
    """What is removable, without removing anything. Returns
    {"worktrees": [...], "kept_live": [...], "tds": [...], "runs": [...],
     "runs_protected": [...]} — all Paths."""
    repo = Path(repo).resolve()
    # bookkeeping first: entries whose dir is already gone stop shadowing names
    vcs.git(repo, "worktree", "prune")
    live = _registered_worktrees(repo)

    wt_parent = repo.parent / ".aro-worktrees"
    orphans, kept = [], []
    for child in sorted(wt_parent.iterdir()) if wt_parent.is_dir() else []:
        if not child.is_dir():
            continue
        if str(child.resolve()) in live and not registered:
            kept.append(child)
        else:
            orphans.append(child)

    # a td dir is orphaned when its worktree dir no longer exists (or is being
    # removed in this pass)
    td_root = repo.parent / f".aro-{spec_name}-td"
    going = {c.name for c in orphans}
    tds = []
    for child in sorted(td_root.iterdir()) if td_root.is_dir() else []:
        if child.is_dir() and (child.name in going
                               or not (wt_parent / child.name).exists()):
            tds.append(child)

    runs, protected = [], []
    if runs_dir is not None and Path(runs_dir).is_dir():
        keep = _ledger_protected_names()
        for child in sorted(Path(runs_dir).iterdir()):
            if not child.is_dir():
                continue
            (protected if child.name in keep else runs).append(child)
    return {"worktrees": orphans, "kept_live": kept, "tds": tds,
            "runs": runs, "runs_protected": protected}


def cli(args) -> None:
    from . import spec as specmod
    sp = specmod.load(args.spec)
    repo = Path(sp.repo).resolve()
    found = scan(repo, sp.name,
                 runs_dir=(Path(args.runs) if args.runs else None),
                 registered=args.registered)

    for w in found["kept_live"]:
        print(f"keep (registered worktree — live or crashed; --registered removes): {w}")
    for r in found["runs_protected"]:
        print(f"keep (referenced by a permanent ledger): {r}")

    doomed = found["worktrees"] + found["tds"] + found["runs"]
    if not doomed:
        print("clean: nothing to remove")
        return
    for p in doomed:
        print(f"{'would remove' if args.dry_run else 'remove'}: {p}")
    if args.dry_run:
        print(f"dry-run: {len(doomed)} path(s) would be removed")
        return
    for w in found["worktrees"]:
        vcs.worktree_remove(repo, w)          # unregister if needed, then rmtree
    for p in found["tds"] + found["runs"]:
        shutil.rmtree(p, ignore_errors=True)
    print(f"clean: removed {len(doomed)} path(s) "
          f"({len(found['worktrees'])} worktree(s), {len(found['tds'])} target dir(s), "
          f"{len(found['runs'])} run dir(s))")
