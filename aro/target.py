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
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import context as ctxmod
from . import vcs
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

    def td_for(self, work) -> Path:
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

    @property
    def has_differential(self) -> bool:
        return bool(self.spec.differential)

    @property
    def differential_required(self) -> bool:
        """Strict by default: a candidate must prove byte-identical behaviour with a
        random-input differential. Only `constraints.weak_oracle=true` downgrades to
        the test-suite-only check (and the judge flags the verdict)."""
        return not bool(self.spec.constraints.get("weak_oracle"))

    def make_worktree(self, tag: str) -> Path:
        self._worktree_parent.mkdir(parents=True, exist_ok=True)
        path = self._worktree_parent / f"{tag}-{time.monotonic_ns()}"
        vcs.worktree_add(self.repo, path, self.baseline_sha)
        # Populate submodules: `git worktree add` does NOT check them out, but a repo
        # with a build.rs that consumes a submodule (e.g. a forge-std-driven codegen
        # step) won't even build without it. Offline (clones from the repo's local
        # object store), best-effort — a repo with no submodules is a no-op.
        if (self.repo / ".gitmodules").exists():
            vcs.submodule_update(path, timeout=self.spec.timeout)
        return path

    def remove_worktree(self, work: Path) -> None:
        vcs.worktree_remove(self.repo, work)
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
        out = subprocess.run(self.spec.build, cwd=str(work), env=self.env_for(work),
                             capture_output=True, text=True, timeout=self.spec.timeout)
        combined = (out.stdout or "") + (out.stderr or "")
        if out.returncode != 0:
            raise RuntimeError(_tail(combined, 40))
        return combined

    def scoped_clean(self, work: Path) -> bool:
        """Force the next build to recompile the edited crate by dropping its
        artifacts from this worktree's target dir — a STRUCTURED recompile guarantee
        (robust to `cargo build -q`, caches, output format, and a future non-cargo
        build) instead of grepping cargo stdout for `Compiling`. Best-effort: returns
        False if no clean ran, so the caller can fall back to the stdout heuristic."""
        pkgs = {self.spec.bench.get("pkg")}
        if self.spec.differential.get("pkg"):
            pkgs.add(self.spec.differential["pkg"])
        ok = False
        for pkg in filter(None, pkgs):
            out = subprocess.run(["cargo", "clean", "--release", "-p", pkg],
                                 cwd=str(work), env=self.env_for(work),
                                 capture_output=True, text=True, timeout=self.spec.timeout)
            ok = ok or out.returncode == 0
        return ok

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
            # No probe declared: nothing to compare. This path is only reachable
            # under constraints.weak_oracle=true — eval refuses strict targets with
            # no differential and flags weak-oracle verdicts (WEAK ORACLE note).
            return True
        base_fp = self.run_diff_probe(baseline, d)
        cand_fp = self.run_diff_probe(work, d)
        if not base_fp or not cand_fp:
            raise RuntimeError("differential probe produced no output")
        return base_fp == cand_fp

    def run_diff_probe(self, work: Path, d: dict) -> Optional[str]:
        ex = self.pkg_dir(work, d["pkg"]) / "examples" / f"{d['example']}.rs"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(self.spec.diff_probe_src())
        out = self._cargo_run(work, d["pkg"], d["example"])
        for line in out.splitlines():
            if line.startswith(d["prefix"]):
                return line.strip()
        return None

    def bench(self, work: Path, scale: int = 1) -> Metrics:
        b = self.spec.bench
        self.write_probe(work, b["pkg"], b["example"])
        out = self._cargo_run(work, b["pkg"], b["example"], scale=scale)
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

    def icount(self, work: Path, scale: int = 1, cache_sim: bool = False):
        """Whole-process instruction count for the bench probe under callgrind.

        Mirrors `bench()`: same probe, same ARO_BENCH_SCALE. Builds the example
        once via cargo, then runs the BINARY under valgrind directly (never
        valgrind-wrap cargo — that would attribute build-tool Ir). Returns an
        `ICountResult` (Ir + full event map + profile_fingerprint).
        """
        from . import icount as icmod
        cargo_toml = Path(work) / "Cargo.toml"
        try:
            toml_text = cargo_toml.read_text() if cargo_toml.exists() else ""
        except Exception as e:
            raise RuntimeError(f"icount: cannot read Cargo.toml: {e}")
        mode = getattr(self.spec, "profile_fidelity", None) or "codspeed-ci"
        baseline_text = None
        if mode == "repo-release":
            # Baseline pin's Cargo.toml is the production-profile truth. Worktrees
            # are cut from baseline_sha; comparing against the repo pin catches
            # candidate/operator drift at the measurement seam.
            try:
                base_cargo = Path(self.repo) / "Cargo.toml"
                baseline_text = (base_cargo.read_text()
                                 if base_cargo.exists() else "")
            except Exception as e:
                raise RuntimeError(
                    f"icount: cannot read baseline Cargo.toml: {e}")
        bad = icmod.check_profile_fidelity(
            toml_text, mode=mode, baseline_cargo_toml_text=baseline_text)
        if bad:
            raise RuntimeError(bad)
        rustc_v = self._rustc_version(work)
        fp = icmod.profile_fingerprint(toml_text, rustc_v)

        binary = self.build_example(work)
        if not Path(binary).exists():
            raise RuntimeError(f"icount: probe binary missing at {binary}")

        # Fresh output dir per run so concurrent/baseline+cand pairs never collide.
        out_dir = Path(tempfile.mkdtemp(prefix="aro-callgrind-"))
        out_file = out_dir / "callgrind.out"
        cmd = ["valgrind", "--tool=callgrind",
               f"--callgrind-out-file={out_file}"]
        if cache_sim:
            cmd.append("--cache-sim=yes")
        cmd.append(str(binary))
        env = self.env_for(work)
        # Lowest useful iteration scale: valgrind is ~10–50× slower than bare
        # release; same scale on both sides keeps ΔIr% comparable.
        env["ARO_BENCH_SCALE"] = str(scale)
        try:
            out = subprocess.run(cmd, cwd=str(work), env=env,
                                 capture_output=True, text=True,
                                 timeout=self.spec.timeout)
        except subprocess.TimeoutExpired:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError(f"icount: valgrind timed out after {self.spec.timeout}s")
        if out.returncode != 0:
            shutil.rmtree(out_dir, ignore_errors=True)
            err = out.stderr if (out.stderr or "").strip() else out.stdout
            raise RuntimeError(_tail(err or "valgrind failed", 40))
        if not out_file.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError(f"icount: callgrind did not write {out_file}")
        try:
            events = icmod.parse_callgrind_totals(out_file.read_text())
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
        if "Ir" not in events:
            raise RuntimeError("icount: callgrind totals missing Ir event")
        return icmod.ICountResult(ir=events["Ir"], events=events,
                                  profile_fingerprint=fp)

    def _rustc_version(self, work: Path) -> str:
        # cwd=work so rustc -V honours the worktree's rust-toolchain.toml pin
        # instead of whatever ambient toolchain the host shell happens to have.
        try:
            out = subprocess.run(["rustc", "-V"], capture_output=True, text=True,
                                 timeout=30, cwd=str(work))
            return (out.stdout or out.stderr or "").strip()
        except Exception:
            return "rustc-unknown"

    def compute_region_hint(self, work: Path):
        """Profiler-grounded hint from external prompt templates. `blind` picks the
        profiler-only variant. The relevant code (spec.context anchors) is attached
        so even a blind run has the materials to derive the change itself."""
        p = self.spec.profile
        try:
            binary = self.build_example(work)
        except Exception:
            binary = self.td_for(work) / "release" / "examples" / \
                p.get("example", self.spec.bench["example"])
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
        return vcs.rev_parse(self.repo, ref) or ref

    def env_for(self, work):
        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(self.td_for(work))
        # Symbol-rich target builds. Many repos strip release binaries (mega-evm:
        # debug="none" + strip="symbols"), which leaves Linux `perf` nothing to
        # attribute samples to — the L1 frontier collapses into one surviving
        # internal blob (macOS is immune: Mach-O function starts survive strip).
        # Debug info + kept symbols do not change optimized codegen, so bench
        # numbers are unaffected; applied to EVERY target build (not just the
        # profile example) so all builds share one cargo fingerprint and the
        # bench/test/profile cycle never thrashes full rebuilds. setdefault: an
        # operator's explicit setting wins.
        env.setdefault("CARGO_PROFILE_RELEASE_DEBUG", "2")
        env.setdefault("CARGO_PROFILE_RELEASE_STRIP", "none")
        return env

    def _run(self, work: Path, cmd) -> str:
        out = subprocess.run(cmd, cwd=str(work), env=self.env_for(work),
                             capture_output=True, text=True, timeout=self.spec.timeout)
        if out.returncode != 0:
            text = out.stderr if out.stderr.strip() else out.stdout
            raise RuntimeError(_tail(text, 40))
        return out.stdout

    def pkg_dir(self, work: Path, pkg: str) -> Path:
        """Resolve a package NAME to its crate directory inside `work`. Layouts vary
        (`banderwagon/` at the repo root vs `crates/<crate>/` under a workspace), so a
        probe can't assume the dir equals the name. Ask `cargo metadata` once, cache the
        path RELATIVE to the worktree (the layout is identical across worktrees), and
        fall back to `<work>/<pkg>` when metadata is unavailable (the simple layout)."""
        cache = self.__dict__.setdefault("_pkgdir_cache", {})
        if pkg not in cache:
            rel = pkg  # fallback: dir == name (e.g. salt's `banderwagon/`)
            out = subprocess.run(
                ["cargo", "metadata", "--format-version", "1", "--no-deps"],
                cwd=str(work), env=self.env_for(work), capture_output=True, text=True,
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

    def write_probe(self, work: Path, pkg: str, example: str) -> None:
        pkg_dir = self.pkg_dir(work, pkg)
        # Probes rely on cargo's examples/ auto-discovery. With `autoexamples =
        # false` a freshly-dropped file is NOT a target and cargo fails with an
        # unhelpful "no example target" — fail HERE with the actual fix instead.
        manifest = pkg_dir / "Cargo.toml"
        try:
            mtext = manifest.read_text() if manifest.exists() else ""
        except Exception:
            mtext = ""
        if (re.search(r"^\s*autoexamples\s*=\s*false", mtext, re.MULTILINE)
                and not re.search(r'name\s*=\s*"' + re.escape(example) + '"', mtext)):
            raise RuntimeError(
                f"crate `{pkg}` sets autoexamples = false, so the probe example "
                f"`{example}` cannot be auto-discovered. Add an [[example]] stanza "
                f'(name = "{example}", path = "examples/{example}.rs") or remove '
                f"the autoexamples setting")
        ex = pkg_dir / "examples" / f"{example}.rs"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(self.spec.probe_src())

    def _cargo_run(self, work: Path, pkg: str, example: str, scale: int = 1) -> str:
        env = self.env_for(work)
        # The auto-tightening knob: a noise-limited verdict re-benches at a higher
        # scale; a scale-aware probe reads ARO_BENCH_SCALE and multiplies its batch /
        # inner-repeat count, so each timed sample averages more work → a lower A/A
        # floor — WITHOUT changing the path or the inputs. Probes that ignore it just
        # run identically (escalation then can't help → honest noise-limited).
        env["ARO_BENCH_SCALE"] = str(scale)
        out = subprocess.run(
            ["cargo", "run", "--release", "-p", pkg, "--example", example,
             *self.spec.bench.get("cargo_args", [])],
            cwd=str(work), env=env, capture_output=True, text=True,
            timeout=self.spec.timeout)
        if out.returncode != 0:
            raise RuntimeError(_tail(out.stderr if out.stderr.strip() else out.stdout, 40))
        return out.stdout

    def build_example(self, work: Path, example: str = None) -> Path:
        """Build the bench/profile example and return the EXECUTABLE path from
        cargo's own artifact messages (`--message-format=json`), instead of
        assuming `<td>/release/examples/<name>` — that hardcoded guess breaks
        whenever a target triple is in play (`.cargo/config.toml` build.target)
        or cargo changes layout. Falls back to the classic path if no artifact
        message names the example (old cargo, weird pipes)."""
        b = self.spec.bench
        example = example or self.spec.profile.get("example", b["example"])
        if example == b["example"]:
            self.write_probe(work, b["pkg"], example)
        out = subprocess.run(
            ["cargo", "build", "--release", "-p", b["pkg"], "--example", example,
             "--message-format=json", *b.get("cargo_args", [])],
            cwd=str(work), env=self.env_for(work), capture_output=True, text=True,
            timeout=self.spec.timeout)
        if out.returncode != 0:
            err = out.stderr if out.stderr.strip() else out.stdout
            raise RuntimeError(_tail(err, 40))
        exe = _executable_from_cargo_json(out.stdout, example)
        return Path(exe) if exe else \
            self.td_for(work) / "release" / "examples" / example


def _tail(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def _executable_from_cargo_json(stdout: str, example: str) -> Optional[str]:
    """The `executable` of the compiler-artifact message for `example` in a
    `cargo build --message-format=json` stream. Pure, so it is unit-testable
    without cargo. None when absent (caller falls back to the classic path)."""
    import json as _json
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            msg = _json.loads(line)
        except ValueError:
            continue
        if (msg.get("reason") == "compiler-artifact" and msg.get("executable")
                and msg.get("target", {}).get("name") == example
                and "example" in (msg.get("target", {}).get("kind") or [])):
            return msg["executable"]
    return None


def _count_passed(text: str) -> Optional[int]:
    """Sum `test result: ok. N passed` across all test binaries; None if absent."""
    total, found = 0, False
    for m in re.finditer(r"test result: ok\. (\d+) passed", text):
        total += int(m.group(1))
        found = True
    return total if found else None
