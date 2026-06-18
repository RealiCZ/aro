"""SpecTarget — the single, generic target driver.

Replaces the hand-written SaltTarget / CommitterTarget classes: everything
target-specific (build/test/bench commands, the probe, the editable regions, the
profiler harness, the hint) comes from a TargetSpec. Worktree isolation, the
cargo/git plumbing, and the bench/profile parsing are the generic, deterministic
glue that feeds the judge.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import context as ctxmod
from . import profile as profmod
from . import prompts
from .types import Metrics, Objective, Patch


class SpecTarget:
    def __init__(self, spec):
        self.spec = spec
        self.repo = Path(spec.repo).resolve()
        self.baseline_sha = self._resolve_sha(spec.baseline_ref)
        self.target_dir = (self.repo.parent / ".aro-salt-target").resolve()
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self._worktree_parent = (self.repo.parent / ".aro-worktrees").resolve()
        self.blind = spec.blind

    # --- Target interface ----------------------------------------------------

    @property
    def name(self) -> str:
        return self.spec.name

    def objectives(self):
        return [Objective(o["metric"], o.get("minimize", True)) for o in self.spec.objectives]

    def make_worktree(self, tag: str) -> Path:
        self._worktree_parent.mkdir(parents=True, exist_ok=True)
        path = self._worktree_parent / f"{tag}-{time.monotonic_ns()}"
        out = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "--detach",
             str(path), self.baseline_sha],
            capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(_tail(out.stderr, 40))
        return path

    def remove_worktree(self, work: Path) -> None:
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(work)],
                       capture_output=True, text=True)
        shutil.rmtree(work, ignore_errors=True)

    def apply(self, patch: Patch, work: Path) -> None:
        if patch.is_noop:
            return
        for e in patch.edits:
            f = Path(work) / e.path
            content = f.read_text()
            idx = content.find(e.search)
            if idx < 0:
                raise RuntimeError(f"search text not found in {e.path}")
            f.write_text(content[:idx] + e.replace + content[idx + len(e.search):])

    def build(self, work: Path) -> None:
        self._run(work, self.spec.build)

    def test(self, work: Path) -> Optional[int]:
        """Run the correctness suite. Raises on failure; on success returns the
        number of passing tests (parsed from cargo's `test result: ok. N passed`)
        so the engine can enforce a regression gate — a candidate that still exits
        0 but drops below the baseline pass count is auto-discarded. None when the
        count can't be parsed (the regression gate then degrades to off)."""
        return _count_passed(self._run(work, self.spec.test))

    def differential(self, work: Path, baseline: Path) -> bool:
        # MVP: clean tree (NoOp) trivially identical; dirty tree leans on the test
        # gate. Real random-input differential fuzz belongs as a probe in `probes/`
        # named by the spec (TODO).
        out = subprocess.run(["git", "-C", str(work), "status", "--porcelain"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(_tail(out.stderr, 40))
        return True

    def bench(self, work: Path) -> Metrics:
        b = self.spec.bench
        self._write_probe(work, b["pkg"], b["example"])
        out = self._cargo_run(work, b["pkg"], b["example"])
        samples = None
        for line in out.splitlines():
            if line.startswith(b["sample_prefix"]):
                samples = [float(x) for x in line.split()[1:]]
        if not samples:
            raise RuntimeError(f"probe produced no '{b['sample_prefix']}' samples")
        m = Metrics()
        m.put(b["metric"], samples)
        return m

    def compute_region_hint(self, work: Path):
        """Profiler-grounded hint from external prompt templates. `blind` picks the
        profiler-only variant. The relevant code (spec.context anchors) is attached
        so even a blind run has the materials to derive the change itself."""
        p = self.spec.profile
        binary = self.target_dir / "release" / "examples" / p.get("example", self.spec.bench["example"])
        funcs = profmod.top_functions(binary, spin_secs=p.get("spin_secs", 8),
                                      sample_secs=p.get("sample_secs", 4))
        top = ", ".join(f"{n} {pc:.0f}%" for n, _, pc in funcs[:3]) if funcs else "(hot fn)"
        anchors = [tuple(a) for a in self.spec.context.get("anchors", [])]
        code = ctxmod.extract(Path(work) / self.spec.context["file"], anchors) \
            if self.spec.context.get("file") else ""
        code_block = ("\nRelevant code (data structure, how it is built, hot "
                      "function):\n```rust\n" + code + "\n```") if code else ""
        name = self.spec.prompts["hint_blind"] if self.blind else self.spec.prompts["hint"]
        return prompts.load(name, top=top, code=code_block)

    # --- internals -----------------------------------------------------------

    def _resolve_sha(self, ref: str) -> str:
        out = subprocess.run(["git", "-C", str(self.repo), "rev-parse", ref],
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ref

    def _env(self):
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(self.target_dir)
        return env

    def _run(self, work: Path, cmd) -> str:
        out = subprocess.run(cmd, cwd=str(work), env=self._env(),
                             capture_output=True, text=True)
        if out.returncode != 0:
            text = out.stderr if out.stderr.strip() else out.stdout
            raise RuntimeError(_tail(text, 40))
        return out.stdout

    def _write_probe(self, work: Path, pkg: str, example: str) -> None:
        ex = Path(work) / pkg / "examples" / f"{example}.rs"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(self.spec.probe_src())

    def _cargo_run(self, work: Path, pkg: str, example: str) -> str:
        out = subprocess.run(
            ["cargo", "run", "--release", "-p", pkg, "--example", example],
            cwd=str(work), env=self._env(), capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(_tail(out.stderr if out.stderr.strip() else out.stdout, 40))
        return out.stdout


def _tail(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def _count_passed(text: str) -> Optional[int]:
    """Sum `test result: ok. N passed` across all test binaries; None if absent."""
    total, found = 0, False
    for m in re.finditer(r"test result: ok\. (\d+) passed", text):
        total += int(m.group(1))
        found = True
    return total if found else None
