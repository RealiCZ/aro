"""CPU profiler (the observe arm).

Finds where the time actually goes, autonomously: it runs a hot-loop binary,
samples it, and returns the heaviest *compute* functions (filtering out idle/wait
frames and system libraries). The top function is fed into the generator's region
hint so it optimizes the measured hot path rather than readable-but-cold code.

Cross-platform flat self-time sampling (`_raw_samples`): macOS uses the built-in
`sample` (no sudo); Linux uses `perf` (needs perf installed and
`kernel.perf_event_paranoid <= 1`, or CAP_PERFMON/root). Both yield
`[(mangled_symbol, image, count)]`; we keep target-binary symbols, best-effort
demangle Rust v0 names, and rank by count. Any failure (missing tool, perms)
degrades to an empty profile — the caller then reports "no profile".
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Frames that are idle/scheduling/system noise, not the workload's compute.
_DROP_IMAGES = ("libsystem_", "libdyld", "dyld", "libobjc", "libc++",  # macOS
                "libc.so", "ld-linux", "ld.so", "libpthread", "libgcc", "libm.so",  # Linux
                "[kernel", "[vdso", "[unknown")
_SKIP_NAMES = {"mod", "ops", "arith", "core", "alloc", "models", "fields",
               "fp", "group", "curves", "raw_vec", "ark_ff",
               "ark_ec", "num_bigint", "std"}


def demangle(sym: str) -> str:
    """Best-effort readable name from a Rust v0 mangled symbol. v0 names are an
    unseparated run of length-prefixed identifiers (`7mycrate8do_thing`),
    so we scan length prefixes and read exactly that many chars, then pick the
    last snake_case-looking fragment (the function name). Non-`_R` symbols pass
    through."""
    if not sym.startswith("_R"):
        return sym
    names, i = [], 0
    while i < len(sym):
        if sym[i].isdigit():
            j = i
            while j < len(sym) and sym[j].isdigit():
                j += 1
            n = int(sym[i:j])
            frag = sym[j:j + n]
            if frag and (frag[0].isalpha() or frag[0] == "_"):
                names.append(frag)
            i = j + n
        else:
            i += 1
    ident = re.compile(r"^[a-z][a-z0-9_]*$")  # clean snake_case (drops backref residue)

    def ok(f):
        return bool(ident.match(f)) and f not in _SKIP_NAMES
    # Prefer the last snake_case (underscore) fragment — function names like
    # do_work / step_once / add_assign — over bare crate/type tails.
    for frag in reversed(names):
        if ok(frag) and "_" in frag:
            return frag
    for frag in reversed(names):
        if ok(frag):
            return frag
    return names[-1] if names else sym


def spin_and_sample(binary, spin_secs: int = 8, sample_secs: int = 4):
    """Run the probe long enough to sample, then return raw `[(symbol, image, count)]`.

    The probe is a fixed-iteration microbench that exits in milliseconds at scale 1 — far
    too fast to sample reliably. It honors `ARO_BENCH_SCALE` (multiplies the per-tx hot-loop
    count), so we run it at a high scale to keep it in the hot loop through the whole sample
    window, bumping the scale if it still exits before we can attach. Scale changes the
    iteration count, not WHICH code is hot, so the ranking is unaffected (if anything the hot
    path dominates more). [] if no scale keeps it alive (or the sampler fails)."""
    binary = Path(binary)
    for scale in (16, 64, 256):
        env = dict(os.environ)
        env["ARO_BENCH_SCALE"] = str(scale)
        try:
            proc = subprocess.Popen([str(binary), str(spin_secs)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return []
        time.sleep(1.0)  # let it pass warmup into the hot loop
        if proc.poll() is not None:   # already exited — too fast, scale up and retry
            proc.kill()
            continue
        try:
            raw = _raw_samples(proc.pid, sample_secs)
        finally:
            proc.kill()
        if raw:
            return raw
    return []


def top_functions(binary, spin_secs: int = 8, sample_secs: int = 4, top: int = 8):
    """Profile `binary` and return `[(name, self_samples, in_binary_pct), ...]` over the
    in-binary compute frames, heaviest first. Empty on any failure (missing profiler/perms)."""
    raw = spin_and_sample(binary, spin_secs, sample_secs)
    rows = [(demangle(sym), cnt) for sym, image, cnt in raw
            if not any(d in image for d in _DROP_IMAGES)]
    total = sum(c for _, c in rows) or 1
    rows.sort(key=lambda r: r[1], reverse=True)
    return [(name, c, 100.0 * c / total) for name, c in rows[:top]]


def _raw_samples(pid: int, secs: int):
    """Flat self-time samples of a running pid for ~`secs` seconds, as
    `[(mangled_symbol, image, count)]`. macOS: `sample`; Linux: `perf`. [] on failure."""
    return _samples_macos(pid, secs) if sys.platform == "darwin" else _samples_perf(pid, secs)


def _samples_macos(pid: int, secs: int):
    # Per-call temp file: two concurrent profiles (parallel campaigns, probe
    # qualification while a sweep runs) must never clobber each other's samples.
    fd, name = tempfile.mkstemp(prefix="aro_sample_", suffix=".txt")
    os.close(fd)
    out_file = Path(name)
    try:
        subprocess.run(["/usr/bin/sample", str(pid), str(secs), "-file", str(out_file)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=secs + 30)
        text = out_file.read_text()
    except Exception:
        return []
    finally:
        out_file.unlink(missing_ok=True)
    # the flat "Sort by top of stack" section: `<mangled>  (in <image>)  <count>`
    if "Sort by top of stack" in text:
        text = text.split("Sort by top of stack", 1)[1]
    if "Binary Images:" in text:
        text = text.split("Binary Images:", 1)[0]
    rows, line_re = [], re.compile(r"^\s*(\S+)\s+\(in ([^)]+)\)\s+(\d+)\s*$")
    for line in text.splitlines():
        m = line_re.match(line)
        if m:
            rows.append((m.group(1), m.group(2), int(m.group(3))))
    return rows


def _samples_perf(pid: int, secs: int):
    """Linux `perf`: record the pid flat for `secs`, then read per-symbol overhead.
    Needs perf installed + `kernel.perf_event_paranoid <= 1` (else record fails → [])."""
    if not shutil.which("perf"):
        return []
    fd, data = tempfile.mkstemp(prefix="aro_perf_", suffix=".data")
    os.close(fd)
    try:
        rec = subprocess.run(
            ["perf", "record", "-q", "-o", data, "-F", "997", "-p", str(pid),
             "--", "sleep", str(secs)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=secs + 30)
        if rec.returncode != 0:
            return []  # usually perf_event_paranoid / missing perms
        out = subprocess.run(
            ["perf", "report", "-q", "-i", data, "--stdio", "--no-demangle",
             "--percent-limit", "0", "-F", "overhead,dso,symbol"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return []
    finally:
        Path(data).unlink(missing_ok=True)
    # `   12.34%  <dso>  [.] <mangled-symbol>` — weight ∝ overhead (counts aren't needed,
    # only relative self-time, which the caller turns back into a percentage).
    rows, line_re = [], re.compile(r"^\s*([\d.]+)%\s+(\S+)\s+\[[^\]]*\]\s+(\S+)")
    for line in out.splitlines():
        m = line_re.match(line)
        if m:
            rows.append((m.group(3), m.group(2), max(1, int(float(m.group(1)) * 100))))
    return rows
