"""aro sweep — the frontier-map meta-loop (deterministic, terminating core).

Profile a workload, rank the hot functions, bucket each by OWNER (our crate vs an
external crate / crypto) and by what the cross-run lessons already recorded, and
emit a FRONTIER MAP: where the time goes, what is our lever vs untouchable, what has
been tried (and the judge's verdict), and the actionable frontier — the untried
in-crate functions, heaviest first.

This is the terminating, deterministic skeleton. Per-function OPTIMIZATION attempts
are the existing per-target loop (`aro run` / the autonomous protocol), which this
map surfaces and orders; an accepted change folds into the baseline, and re-running
the sweep re-profiles on top of it (compounding). The sweep terminates because the
hot-function set is finite — it converges to a map, it does not explore forever.

    python3 -m aro sweep <spec.json> [--out report.md] [--min-pct 1.5] [--top N]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import lessons as lessonsmod
from . import profile as profmod
from . import spec as specmod
from .target import SpecTarget

# Symbol markers. "Ours" is decided per-spec (the target crate's name). These tag the
# rest so the report can say WHY a heavy frame is not our lever.
_CRYPTO = ("keccak", "sha3", "p1600", "blake", "secp", "k256", "bn254", "bls12",
           "sha256", "ripemd", "modexp")
_RUNTIME = ("revm", "alloy", "op_revm", "op_alloy", "hashbrown", "foldhash", "ruint",
            "ark_", "num_bigint", "raw_vec", "hashmap", "btree", "core", "alloc", "std")


def _crate_token(pkg: str) -> str:
    """`mega-evm` package → the `mega_evm` token that appears in its mangled symbols."""
    return (pkg or "").replace("-", "_")


# Fragments that are crate names / module paths / generic-arg noise, never the function
# name itself — excluded when picking the readable leaf out of a v0 mangled symbol.
_NAME_NOISE = set(_RUNTIME) | set(_CRYPTO) | {
    "evm", "limit", "instructions", "host", "contract", "control", "interpreter",
    "context", "journal", "external", "primitives", "bits", "stack", "memory",
    "inner", "info", "state", "frame", "tx", "result", "spec", "types", "ext"}


def _fn_name(symbol: str, our_token: str, binary: str = "") -> str:
    """Readable leaf function name from a (v0-mangled) symbol. The demangler in
    `profile.py` collapses heavily-monomorphized generics to the trailing INSTANTIATION
    crate (e.g. the probe/binary name) — wrong for our use. Here we scan the
    length-prefixed identifiers and return the LAST snake_case fragment that is not a
    crate / module / generic-arg token, which is reliably the function name
    (`inspect_storage`, `check_limit`, `on_sstore`, `sload`, …)."""
    frags, i = [], 0
    while i < len(symbol):
        if symbol[i].isdigit():
            j = i
            while j < len(symbol) and symbol[j].isdigit():
                j += 1
            n = int(symbol[i:j])
            frag = symbol[j:j + n]
            i = j + n
            if frag and (frag[0].isalpha() or frag[0] == "_"):
                frags.append(frag)
        else:
            i += 1
    excl = _NAME_NOISE | {our_token, binary}
    cand = [f for f in frags
            if re.match(r"^[a-z][a-z0-9_]*$", f) and f not in excl]
    if cand:
        return cand[-1]
    return profmod.demangle(symbol)


def classify_owner(symbol: str, our_token: str):
    """(owner, why) for a (possibly mangled) symbol. owner ∈ {ours, crypto, runtime,
    unknown}. `ours` wins if the target crate's token appears anywhere in the symbol
    (mega-evm functions are generic over revm types, so a plain substring is enough)."""
    s = symbol.lower()
    if our_token and our_token in s:
        return "ours", our_token
    for m in _CRYPTO:
        if m in s:
            return "crypto", m
    for m in _RUNTIME:
        if m in s:
            return "runtime", m
    return "unknown", ""


def _lesson_index(target_name: str) -> list:
    """Relevant lessons (cross-target recall) as `[(text, verdict, gated)]`, where
    `text` is change+note lowercased and `gated` flags an architecture/maintainability
    objection — so a heavy function the judge already ruled on isn't re-queued blindly."""
    out = []
    for r in lessonsmod.recent(target_name, limit=200):
        text = ((r.get("change", "") or "") + " " + (r.get("note", "") or "")).lower()
        gated = any(w in text for w in ("architectur", "gated", "reviewer", "layer",
                                        "single-respons", "should-merge", "维护", "架构"))
        out.append((text, r.get("verdict", ""), gated))
    return out


# Leaf names that are library / generic methods (a demangler collapse of many distinct
# monomorphizations), not a mega-evm-specific lever — aggregated, not listed as actionable.
_GENERIC_LEAVES = {
    "convert", "error", "fast", "get", "get_mut", "insert", "remove", "contains_key",
    "rustc_entry", "entry", "eq", "cmp", "clone", "hash", "fmt", "from", "into",
    "default", "drop", "fold", "next", "index", "deref", "len", "is_empty", "as_ref",
    "reserve", "grow", "extend", "collect", "iter", "map", "unwrap", "expect"}


def bucket_functions(ranked, our_token: str, lessons_idx: list, min_pct: float):
    """Classify the ranked (name, pct, symbol) frames. Aggregates by leaf name (distinct
    monomorphizations of the same function sum up), splits library/generic leaves off as
    a single tally (not actionable domain levers), and classifies the rest of OUR
    functions against the cross-run lessons. Returns a dict of bucket → [rows]."""
    ours_dom, ours_gen, notours = {}, 0.0, {}
    for name, pct, symbol in ranked:
        if pct < min_pct:
            continue
        owner, why = classify_owner(symbol, our_token)
        if owner != "ours":
            notours.setdefault((name, owner, why), 0.0)
            notours[(name, owner, why)] += pct
            continue
        if name in _GENERIC_LEAVES:
            ours_gen += pct
        else:
            ours_dom[name] = ours_dom.get(name, 0.0) + pct

    buckets = {"untried": [], "tried": [], "gated": [], "not_ours": [], "generic_pct": ours_gen}
    for name, pct in sorted(ours_dom.items(), key=lambda kv: kv[1], reverse=True):
        verdicts = [(v, g) for (t, v, g) in lessons_idx if name and name.lower() in t]
        if any(g for _, g in verdicts):
            buckets["gated"].append({"name": name, "pct": pct,
                                     "verdict": next(v for v, g in verdicts if g)})
        elif verdicts:
            buckets["tried"].append({"name": name, "pct": pct, "verdict": verdicts[-1][0]})
        else:
            buckets["untried"].append({"name": name, "pct": pct})
    buckets["not_ours"] = [{"name": n, "pct": p, "owner": o, "why": w}
                           for (n, o, w), p in sorted(notours.items(),
                                                      key=lambda kv: kv[1], reverse=True)]
    return buckets


def render_map(buckets, spec_name: str, profiled: str, min_pct: float) -> str:
    """The frontier-map report (Markdown)."""
    L = [f"# aro sweep — frontier map: {spec_name}", ""]
    L.append(f"_profiled `{profiled}`; in-crate functions ≥ {min_pct:.1f}% self-time._")
    L.append("")

    own = sum(r["pct"] for b in ("untried", "tried", "gated") for r in buckets[b])
    gen = buckets.get("generic_pct", 0.0)
    notours = sum(r["pct"] for r in buckets["not_ours"])
    L.append(f"**Where the time goes (of the ranked frames):** our named functions ≈ "
             f"{own:.0f}% · our generic/library work ≈ {gen:.0f}% (monomorphized "
             f"conversions / map ops — diffuse, not a clean lever) · not-ours ≈ "
             f"{notours:.0f}% (crypto / runtime — untouchable).")
    L.append("")

    L.append("## Actionable frontier — untried in-crate functions (heaviest first)")
    if buckets["untried"]:
        L.append("| % self-time | function | next step |")
        L.append("|---|---|---|")
        for r in buckets["untried"]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | attempt: `aro run` / the "
                     f"autonomous protocol on this hot fn |")
    else:
        L.append("_None. Every in-crate hot function above the threshold has been "
                 "attempted — the clean frontier is exhausted (see below)._")
    L.append("")

    if buckets["tried"]:
        L.append("## Already attempted (the judge ruled)")
        L.append("| % | function | verdict |")
        L.append("|---|---|---|")
        for r in buckets["tried"]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['verdict']} |")
        L.append("")

    if buckets["gated"]:
        L.append("## Blocked — needs a human call (architecture / maintainability)")
        L.append("| % | function | why |")
        L.append("|---|---|---|")
        for r in buckets["gated"]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['verdict']} — a recorded "
                     f"structural / reviewer objection; `accepted` ≠ should-merge |")
        L.append("")

    if buckets["not_ours"]:
        L.append("## Not our lever (untouchable / external)")
        L.append("| % | frame | owner |")
        L.append("|---|---|---|")
        for r in buckets["not_ours"][:12]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['owner']} ({r['why']}) |")
        L.append("")

    if not buckets["untried"]:
        L.append("## Converged — what unblocks the next gain")
        L.append("- **Widen the workload** — a different / broader corpus exposes different "
                 "hot paths; re-run the sweep on it.")
        L.append("- **Climb the lens** — micro-elimination → data-layout → algorithm → a "
                 "structurally-clean cross-cutting refactor (the higher tiers open new space).")
        L.append("- **A human call** on any architecture-gated item above.")
        L.append("")
    return "\n".join(L)


# --- profiling (best-effort; the deterministic core above is what's tested) --------

def profile_ranked(spec, top: int = 40, our_token: str = ""):
    """Build the spec's profile example in an isolated worktree, sample it, and return
    `[(name, pct, symbol)]` heaviest-first over the in-binary compute frames. Empty on
    any failure (the map then reports 'no profile')."""
    import subprocess
    target = SpecTarget(spec)
    work = target.make_worktree("sweep")
    try:
        b = spec.bench
        target._write_probe(work, b["pkg"], b["example"])
        # Build WITH debuginfo: the release profile strips symbols, which would leave the
        # profiler with only PLT stubs and break owner classification (crate token in the
        # mangled name). Force debug + no-strip via env override; keep the per-worktree dir.
        env = dict(target._env(work))
        env["CARGO_PROFILE_RELEASE_DEBUG"] = "2"
        env["CARGO_PROFILE_RELEASE_STRIP"] = "false"
        out = subprocess.run(
            ["cargo", "build", "--release", "-p", b["pkg"], "--example", b["example"]],
            cwd=str(work), env=env, capture_output=True, text=True, timeout=spec.timeout)
        if out.returncode != 0:
            return []
        p = spec.profile
        binary = target._td_for(work) / "release" / "examples" / \
            p.get("example", b["example"])
        rows = _sample_with_symbols(binary, spin=p.get("spin_secs", 8),
                                    secs=p.get("sample_secs", 4), top=top,
                                    our_token=our_token)
        return rows
    except Exception:
        return []
    finally:
        target.remove_worktree(work)


def _sample_with_symbols(binary, spin, secs, top, our_token=""):
    """Like profile.top_functions but KEEPS the raw symbol (for owner classification)
    and extracts a reliable leaf function name (`_fn_name`, not the weak demangler)."""
    import subprocess
    import time
    binary = Path(binary)
    out_file = Path("/tmp/aro_sweep_sample.txt")
    try:
        proc = subprocess.Popen([str(binary), str(spin)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    try:
        time.sleep(1.0)
        subprocess.run(["/usr/bin/sample", str(proc.pid), str(secs), "-file", str(out_file)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=secs + 30)
    except Exception:
        proc.kill(); return []
    finally:
        proc.kill()
    try:
        text = out_file.read_text()
    except Exception:
        return []
    if "Sort by top of stack" in text:
        text = text.split("Sort by top of stack", 1)[1]
    if "Binary Images:" in text:
        text = text.split("Binary Images:", 1)[0]
    rows, line_re = [], re.compile(r"^\s*(\S+)\s+\(in ([^)]+)\)\s+(\d+)\s*$")
    for line in text.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        sym, image, cnt = m.group(1), m.group(2), int(m.group(3))
        if any(d in image for d in ("libsystem_", "libdyld", "dyld")):
            continue
        rows.append((sym, cnt))
    total = sum(c for _, c in rows) or 1
    rows.sort(key=lambda r: r[1], reverse=True)
    bn = Path(binary).name
    return [(_fn_name(s, our_token, bn), 100.0 * c / total, s) for s, c in rows[:top]]


def main(argv) -> None:
    if not argv:
        raise SystemExit("usage: python3 -m aro sweep <spec.json> "
                         "[--out report.md] [--min-pct 1.5] [--top N]")

    def opt(flag, d=None):
        return argv[argv.index(flag) + 1] if flag in argv else d

    spec = specmod.load(argv[0])
    min_pct = float(opt("--min-pct", 1.5))
    top = int(opt("--top", 40))
    our_token = _crate_token(spec.bench.get("pkg", spec.name))

    print(f"=== aro sweep: {spec.name} ===\nprofiling (build + sample) ...")
    ranked = profile_ranked(spec, top=top, our_token=our_token)
    if not ranked:
        print("WARNING: no profile parsed (is the profile example spin-capable?) — "
              "emitting an empty map.")
    buckets = bucket_functions(ranked, our_token, _lesson_index(spec.name), min_pct)
    report = render_map(buckets, spec.name, spec.profile.get("example", spec.bench["example"]),
                        min_pct)

    out = opt("--out")
    if out:
        Path(out).write_text(report + "\n")
        print(f"frontier map → {out}")
    print("\n" + report)
