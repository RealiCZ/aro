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


def bucket_functions(ranked, our_token: str, lessons_idx: list, min_pct: float):
    """Classify each ranked (name, pct, symbol) into the frontier buckets. `ranked`
    is heaviest-first. Returns a dict of bucket → [rows]."""
    buckets = {"untried": [], "tried": [], "gated": [], "not_ours": []}
    for name, pct, symbol in ranked:
        if pct < min_pct:
            continue
        owner, why = classify_owner(symbol, our_token)
        if owner != "ours":
            buckets["not_ours"].append({"name": name, "pct": pct, "owner": owner, "why": why})
            continue
        # ours — has the judge already ruled on this function?
        verdicts = [(v, g) for (t, v, g) in lessons_idx if name and name.lower() in t]
        if any(g for _, g in verdicts):
            buckets["gated"].append({"name": name, "pct": pct,
                                     "verdict": next(v for v, g in verdicts if g)})
        elif verdicts:
            buckets["tried"].append({"name": name, "pct": pct, "verdict": verdicts[-1][0]})
        else:
            buckets["untried"].append({"name": name, "pct": pct})
    return buckets


def render_map(buckets, spec_name: str, profiled: str, min_pct: float) -> str:
    """The frontier-map report (Markdown)."""
    L = [f"# aro sweep — frontier map: {spec_name}", ""]
    L.append(f"_profiled `{profiled}`; in-crate functions ≥ {min_pct:.1f}% self-time._")
    L.append("")

    own = sum(r["pct"] for b in ("untried", "tried", "gated") for r in buckets[b])
    notours = sum(r["pct"] for r in buckets["not_ours"])
    L.append(f"**Where the time goes (of the ranked frames):** ours ≈ {own:.0f}% · "
             f"not-ours ≈ {notours:.0f}% (crypto / runtime — not our lever).")
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

def profile_ranked(spec, top: int = 40):
    """Build the spec's profile example in an isolated worktree, sample it, and return
    `[(name, pct, symbol)]` heaviest-first over the in-binary compute frames. Empty on
    any failure (the map then reports 'no profile')."""
    target = SpecTarget(spec)
    work = target.make_worktree("sweep")
    try:
        b = spec.bench
        target._write_probe(work, b["pkg"], b["example"])
        target._cargo_run(work, b["pkg"], b["example"])  # build the example
        p = spec.profile
        binary = target._td_for(work) / "release" / "examples" / \
            p.get("example", b["example"])
        rows = _sample_with_symbols(binary, spin=p.get("spin_secs", 8),
                                    secs=p.get("sample_secs", 4), top=top)
        return rows
    except Exception:
        return []
    finally:
        target.remove_worktree(work)


def _sample_with_symbols(binary, spin, secs, top):
    """Like profile.top_functions but KEEPS the raw symbol (for owner classification)."""
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
    return [(profmod.demangle(s), 100.0 * c / total, s) for s, c in rows[:top]]


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
    ranked = profile_ranked(spec, top=top)
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
