"""Reward-hacking guard: the evaluator's cheap, deterministic first screen.

A performance optimizer must change the *implementation* and nothing else. Given
the chance, an LLM generator will take shortcuts that beat the metric without
doing the work: swap in a faster library, edit the benchmark that measures it, or
touch the tests that judge it. None are real optimizations, and all have been
seen in the wild (VibeKernel's model reached for `cutlass`; the design doc §1.3).
This screens a proposed patch *before* any worktree is built.

Path-based on purpose — robust, language-agnostic, hard to argue with:
  - no edit may touch the dependency manifest (Cargo.toml / Cargo.lock);
  - no edit may touch the bench harness (benches/) or test suite (tests/);
  - no edit may escape the worktree (absolute paths, or any `..` component).
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Optional

from .types import Patch


def screen(patch: Patch, regions=None) -> Optional[str]:
    """Return None if the patch only touches the implementation (and, when
    `regions` is given, stays within the spec's editable regions), else a string
    naming the first violation. A NoOp always passes."""
    for e in patch.edits:
        reason = _screen_path(e.path, regions)
        if reason:
            return reason
    return None


def _screen_path(path: str, regions=None) -> Optional[str]:
    p = PurePosixPath(path)

    if p.is_absolute():
        return f"edit path `{path}` is absolute (must stay inside the worktree)"

    parts = p.parts
    if ".." in parts:
        return f"edit path `{path}` escapes the worktree via `..`"

    # Directory components only (exclude the filename): a *file* named benches.rs
    # is still implementation.
    for seg in parts[:-1]:
        if seg in ("benches", "tests"):
            return (f"edit path `{path}` touches the {seg}/ harness "
                    f"(the ruler/judge is off-limits)")

    if p.name in ("Cargo.toml", "Cargo.lock"):
        return (f"edit path `{path}` touches the dependency manifest ({p.name}); "
                f"changing deps is not an optimization")

    if regions and not _in_regions(p, regions):
        return (f"edit path `{path}` is outside the spec's editable regions "
                f"{list(regions)}")

    return None


def _in_regions(p: PurePosixPath, regions) -> bool:
    """True if `p` is one of the region files, or sits under a region directory."""
    for r in regions:
        rp = PurePosixPath(r)
        if p == rp or rp in p.parents:
            return True
    return False
