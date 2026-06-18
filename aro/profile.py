"""CPU profiler (the observe arm, done right).

Earlier the observe arm only counted allocations on the *trie* path — which a
profiler later showed is ~1% of an update. The actual hot path is the committer's
elliptic-curve kernel (`mul_index`, ~76%). This module finds that autonomously:
it runs a hot-loop binary, samples it with macOS `sample` (built in, no sudo),
and returns the heaviest *compute* functions (filtering out idle/wait frames and
system libraries). The top function is fed into the generator's region hint so it
optimizes where the time actually is.

`sample`'s "Sort by top of stack" section is a flat self-time profile: one line
per symbol as `<mangled>  (in <image>)  <count>`. We keep only target-binary
symbols, best-effort demangle Rust v0 names, and rank by count.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

# Frames that are idle/scheduling/system noise, not the workload's compute.
_DROP_IMAGES = ("libsystem_", "libdyld", "dyld", "libobjc", "libc++")
_SKIP_NAMES = {"mod", "ops", "arith", "core", "alloc", "models", "fields",
               "fp", "group", "curves", "raw_vec", "banderwagon", "ark_ff",
               "ark_ec", "num_bigint", "std"}


def demangle(sym: str) -> str:
    """Best-effort readable name from a Rust v0 mangled symbol. v0 names are an
    unseparated run of length-prefixed identifiers (`11banderwagon9mul_index`),
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
    # mul_index / mul_assign / add_assign — over bare crate/type tails.
    for frag in reversed(names):
        if ok(frag) and "_" in frag:
            return frag
    for frag in reversed(names):
        if ok(frag):
            return frag
    return names[-1] if names else sym


def top_functions(binary, spin_secs: int = 8, sample_secs: int = 4, top: int = 8):
    """Run `binary <spin_secs>` (it spins so it can be sampled), profile it with
    macOS `sample`, and return `[(name, self_samples, in_binary_pct), ...]` over
    the in-binary compute frames, heaviest first. Empty list on any failure."""
    binary = Path(binary)
    out_file = Path("/tmp/aro_sample.txt")
    try:
        proc = subprocess.Popen([str(binary), str(spin_secs)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    try:
        time.sleep(1.0)  # let it pass warmup into the spin loop
        subprocess.run(["/usr/bin/sample", str(proc.pid), str(sample_secs),
                        "-file", str(out_file)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=sample_secs + 30)
    except Exception:
        proc.kill()
        return []
    finally:
        proc.kill()

    try:
        text = out_file.read_text()
    except Exception:
        return []
    return _parse(text, binary.name, top)


def _parse(text: str, binary_name: str, top: int):
    # Isolate the flat "Sort by top of stack" section.
    if "Sort by top of stack" in text:
        text = text.split("Sort by top of stack", 1)[1]
    if "Binary Images:" in text:
        text = text.split("Binary Images:", 1)[0]

    rows = []
    line_re = re.compile(r"^\s*(\S+)\s+\(in ([^)]+)\)\s+(\d+)\s*$")
    for line in text.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        sym, image, count = m.group(1), m.group(2), int(m.group(3))
        if any(d in image for d in _DROP_IMAGES):
            continue  # idle / system frame
        rows.append((demangle(sym), count))

    total = sum(c for _, c in rows) or 1
    rows.sort(key=lambda r: r[1], reverse=True)
    return [(name, c, 100.0 * c / total) for name, c in rows[:top]]
