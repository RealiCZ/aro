"""SpecTarget — the single, generic target driver.

Replaces hand-written per-target driver classes: everything
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
        # PER-WORKTREE target dirs. A single shared CARGO_TARGET_DIR is WRONG here:
        # cargo reuses the FIRST worktree's build of a path-dependency for every
        # other worktree (it won't recompile the same crate from a different
        # checkout), so baseline and candidate would silently share one binary —
        # making bench Δ and the differential meaningless. Each worktree gets its
        # own dir under `_td_root` (deps recompiled per worktree: the cost of
        # correctness).
        self._td_root = (self.repo.parent / f".aro-{spec.name}-td").resolve()
        self._td_root.mkdir(parents=True, exist_ok=True)
        self._worktree_parent = (self.repo.parent / ".aro-worktrees").resolve()
        self.blind = spec.blind

    def _td_for(self, work) -> Path:
        td = self._td_root / Path(work).name
        td.mkdir(parents=True, exist_ok=True)
        return td

    # --- Target interface ----------------------------------------------------

    @property
    def name(self) -> str:
        return self.spec.name

    def objectives(self):
        return [Objective(o["metric"], o.get("minimize", True)) for o in self.spec.objectives]

    @property
    def regions(self):
        """Editable region paths from the spec — the guard rejects edits outside these."""
        return self.spec.regions

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
        shutil.rmtree(self._td_root / Path(work).name, ignore_errors=True)

    def apply(self, patch: Patch, work: Path) -> None:
        if patch.is_noop:
            return
        for e in patch.edits:
            f = Path(work) / e.path
            content = f.read_text()
            count = content.count(e.search)
            if count != 1:
                what = "not found" if count == 0 else f"found {count}x (must be unique)"
                raise RuntimeError(f"search text {what} in {e.path}")
            idx = content.find(e.search)
            f.write_text(content[:idx] + e.replace + content[idx + len(e.search):])

    def build(self, work: Path) -> str:
        """Compile. Returns cargo's combined output (the `Compiling <crate>` lines
        live in stderr) so the engine can self-check that a changed candidate
        actually recompiled — a guard against the shared-target-dir reuse bug."""
        out = subprocess.run(self.spec.build, cwd=str(work), env=self._env(work),
                             capture_output=True, text=True, timeout=self.spec.timeout)
        combined = (out.stdout or "") + (out.stderr or "")
        if out.returncode != 0:
            raise RuntimeError(_tail(combined, 40))
        return combined

    def test(self, work: Path) -> Optional[int]:
        """Run the correctness suite. Raises on failure; on success returns the
        number of passing tests (parsed from cargo's `test result: ok. N passed`)
        so the engine can enforce a regression gate — a candidate that still exits
        0 but drops below the baseline pass count is auto-discarded. None when the
        count can't be parsed (the regression gate then degrades to off)."""
        return _count_passed(self._run(work, self.spec.test))

    def differential(self, work: Path, baseline: Path) -> bool:
        """Byte-identical behaviour check. If the spec declares a `differential`
        probe, run that SAME deterministic random-input probe in BOTH the baseline
        and candidate worktrees and require identical output — a real behaviour
        guarantee for crypto/consensus code, beyond the test suite. With no probe
        declared, fall back to the (clean-tree) MVP."""
        d = self.spec.differential
        if not d:
            out = subprocess.run(["git", "-C", str(work), "status", "--porcelain"],
                                 capture_output=True, text=True)
            if out.returncode != 0:
                raise RuntimeError(_tail(out.stderr, 40))
            return True
        base_fp = self._run_diff_probe(baseline, d)
        cand_fp = self._run_diff_probe(work, d)
        if not base_fp or not cand_fp:
            raise RuntimeError("differential probe produced no output")
        return base_fp == cand_fp

    def _run_diff_probe(self, work: Path, d: dict) -> Optional[str]:
        ex = self._pkg_dir(work, d["pkg"]) / "examples" / f"{d['example']}.rs"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(self.spec.diff_probe_src())
        out = self._cargo_run(work, d["pkg"], d["example"])
        for line in out.splitlines():
            if line.startswith(d["prefix"]):
                return line.strip()
        return None

    def bench(self, work: Path) -> Metrics:
        b = self.spec.bench
        self._write_probe(work, b["pkg"], b["example"])
        out = self._cargo_run(work, b["pkg"], b["example"])
        samples = None
        for line in out.splitlines():
            if line.startswith(b["sample_prefix"]):
                # Take the leading numeric tokens after the prefix and stop at the
                # first non-numeric one, so a probe may append human labels/metadata
                # (e.g. `BENCH 0.92 ns_per_call iters=50000000`) without breaking the
                # parse. A bare `BENCH f1 f2 f3 ...` still yields all samples.
                vals = []
                for tok in line.split()[1:]:
                    try:
                        vals.append(float(tok))
                    except ValueError:
                        break
                if vals:
                    samples = vals
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
        binary = self._td_for(work) / "release" / "examples" / p.get("example", self.spec.bench["example"])
        funcs = profmod.top_functions(binary, spin_secs=p.get("spin_secs", 8),
                                      sample_secs=p.get("sample_secs", 4))
        top = ", ".join(f"{n} {pc:.0f}%" for n, _, pc in funcs[:3]) if funcs else "(hot fn)"
        anchors = [tuple(a) for a in self.spec.context.get("anchors", [])]
        code = ctxmod.extract(Path(work) / self.spec.context["file"], anchors) \
            if self.spec.context.get("file") else ""
        code_block = ("\nRelevant code (data structure, how it is built, hot "
                      "function):\n```rust\n" + code + "\n```") if code else ""
        name = self.spec.prompts["hint_blind"] if self.blind else self.spec.prompts["hint"]
        try:
            return prompts.load(name, top=top, code=code_block)
        except FileNotFoundError:
            # The region hint is the best-effort "observe arm" — a PlannedGenerator
            # replay ignores it entirely. A missing/renamed template must never crash
            # a whole judged run: degrade to a plain profiler-derived hint instead.
            return f"Profiler-measured hot path: {top}.{code_block}"

    # --- internals -----------------------------------------------------------

    def _resolve_sha(self, ref: str) -> str:
        out = subprocess.run(["git", "-C", str(self.repo), "rev-parse", ref],
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ref

    def _env(self, work):
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(self._td_for(work))
        return env

    def _run(self, work: Path, cmd) -> str:
        out = subprocess.run(cmd, cwd=str(work), env=self._env(work),
                             capture_output=True, text=True, timeout=self.spec.timeout)
        if out.returncode != 0:
            text = out.stderr if out.stderr.strip() else out.stdout
            raise RuntimeError(_tail(text, 40))
        return out.stdout

    def _pkg_dir(self, work: Path, pkg: str) -> Path:
        """Resolve a package NAME to its crate directory inside `work`. Layouts vary
        (`banderwagon/` at the repo root vs `crates/mega-evm/` under a workspace), so a
        probe can't assume the dir equals the name. Ask `cargo metadata` once, cache the
        path RELATIVE to the worktree (the layout is identical across worktrees), and
        fall back to `<work>/<pkg>` when metadata is unavailable (the simple layout)."""
        cache = self.__dict__.setdefault("_pkgdir_cache", {})
        if pkg not in cache:
            rel = pkg  # fallback: dir == name (e.g. salt's `banderwagon/`)
            out = subprocess.run(
                ["cargo", "metadata", "--format-version", "1", "--no-deps"],
                cwd=str(work), env=self._env(work), capture_output=True, text=True,
                timeout=self.spec.timeout)
            if out.returncode == 0:
                import json
                for p in json.loads(out.stdout).get("packages", []):
                    if p.get("name") == pkg:
                        d = Path(p["manifest_path"]).parent
                        try:
                            rel = str(d.relative_to(Path(work).resolve()))
                        except ValueError:
                            rel = str(d)
                        break
            cache[pkg] = rel
        d = Path(cache[pkg])
        return d if d.is_absolute() else Path(work) / d

    def _write_probe(self, work: Path, pkg: str, example: str) -> None:
        ex = self._pkg_dir(work, pkg) / "examples" / f"{example}.rs"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(self.spec.probe_src())

    def _cargo_run(self, work: Path, pkg: str, example: str) -> str:
        out = subprocess.run(
            ["cargo", "run", "--release", "-p", pkg, "--example", example],
            cwd=str(work), env=self._env(work), capture_output=True, text=True,
            timeout=self.spec.timeout)
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
