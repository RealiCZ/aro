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

import dataclasses
import json
import re
import shutil
from pathlib import Path

from . import eval as evalmod
from . import lessons as lessonsmod
from . import profile as profmod
from . import spec as specmod
from .target import SpecTarget
from .types import Patch, best_improvement

# Symbol markers. "Ours" is decided per-spec (the target crate's name). These tag the
# rest so the report can say WHY a heavy frame is not our lever.
_CRYPTO = ("keccak", "sha3", "p1600", "blake", "secp", "k256", "bn254", "bls12",
           "sha256", "ripemd", "modexp")
_RUNTIME = ("revm", "alloy", "op_revm", "op_alloy", "hashbrown", "foldhash", "ruint",
            "ark_", "num_bigint", "raw_vec", "hashmap", "btree", "core", "alloc", "std")


def _crate_token(pkg: str) -> str:
    """`mega-evm` package → the `mega_evm` token that appears in its mangled symbols."""
    return (pkg or "").replace("-", "_")


def _workspace_members(target) -> list:
    """Names of ALL workspace member crates (the editable 'ours' set) via
    `cargo metadata --no-deps`, cached on the target. Stage-1: 'ours' is the whole
    workspace, not just the bench pkg — so a hot fn in a sibling crate (e.g.
    ipa-multipoint) is still ours/editable, not classified 'unknown'. [] on failure."""
    cached = target.__dict__.get("_ws_members")
    if cached is not None:
        return cached
    names = []
    try:
        import json
        import subprocess
        out = subprocess.run(["cargo", "metadata", "--format-version", "1", "--no-deps"],
                             cwd=str(target.repo), capture_output=True, text=True,
                             timeout=getattr(target.spec, "timeout", 600))
        if out.returncode == 0:
            names = [p["name"] for p in json.loads(out.stdout).get("packages", [])]
    except Exception:
        names = []
    target.__dict__["_ws_members"] = names
    return names


def _workspace_tokens(target, fallback_pkg: str = "") -> set:
    """The 'ours' token set = every workspace member's mangled-symbol token; falls back
    to the bench pkg alone when cargo metadata is unavailable."""
    toks = {_crate_token(n) for n in _workspace_members(target)}
    if not toks and fallback_pkg:
        toks = {_crate_token(fallback_pkg)}
    return toks


# Fragments that are crate names / module paths / generic-arg noise, never the function
# name itself — excluded when picking the readable leaf out of a v0 mangled symbol.
_NAME_NOISE = set(_RUNTIME) | set(_CRYPTO) | {
    "evm", "limit", "instructions", "host", "contract", "control", "interpreter",
    "context", "journal", "external", "primitives", "bits", "stack", "memory",
    "inner", "info", "state", "frame", "tx", "result", "spec", "types", "ext"}


def _inst_crate(symbol: str):
    """The trailing MONOMORPHIZATION-INSTANTIATION crate of a v0 symbol, recorded as
    `…Cs<base62>_<len><cratename>` at the very end (e.g. the probe/example binary,
    `sweep_hotloop`). It is NOT the function — a generic fn like `inspect_storage`
    carries it as a suffix, so picking the trailing fragment mislabels every
    monomorphized lever as the binary crate. Return that cratename so it is excluded."""
    m = re.search(r"Cs[0-9A-Za-z]+_\d+([a-z][a-z0-9_]*)$", symbol)
    return m.group(1) if m else None


def _fn_name(symbol: str, our_token: str, binary: str = "") -> str:
    """Readable leaf function name from a (v0-mangled) symbol. We scan the
    length-prefixed identifiers and return the LAST snake_case fragment that is not a
    crate / module / generic-arg token NOR the trailing instantiation crate — which is
    reliably the function name (`inspect_storage`, `check_limit`, `sstore`, `sload`, …).
    Stripping the instantiation crate is essential: without it a generic in-crate lever
    (`…inspect_storage Cs…_13sweep_hotloop`) is mislabeled as the binary and collapsed
    into one un-locatable frame — the bug that hid the real levers from the explorer."""
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
    inst = _inst_crate(symbol)
    ours = {our_token} if isinstance(our_token, str) else set(our_token or [])
    excl = _NAME_NOISE | ours | {binary} | ({inst} if inst else set())
    cand = [f for f in frags
            if re.match(r"^[a-z][a-z0-9_]*$", f) and f not in excl]
    if cand:
        return cand[-1]
    return profmod.demangle(symbol)


def _have_rustfilt():
    """Cached `rustfilt` path (the canonical rustc-demangle CLI), or None — then the
    in-house `_fn_name` heuristic is the fallback (zero hard dependency)."""
    c = _have_rustfilt.__dict__
    if "v" not in c:
        c["v"] = shutil.which("rustfilt")
    return c["v"]


def _split_top(s: str) -> list:
    """Split a demangled path on `::` at angle-bracket depth 0 (so `::` inside generic
    args `<…>` doesn't split)."""
    parts, depth, last, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c == "<":
            depth += 1
        elif c == ">":
            depth = max(0, depth - 1)
        elif c == ":" and depth == 0 and i + 1 < len(s) and s[i + 1] == ":":
            parts.append(s[last:i])
            i += 2
            last = i
            continue
        i += 1
    parts.append(s[last:])
    return parts


def _demangle_leaf(demangled: str) -> str:
    """Function-leaf name from a rustfilt-demangled path. The function name is the last
    top-level `::` segment that is a plain identifier (not a `<…>` Self-type or trailing
    turbofish): `<Journal<…> as …Tr>::inspect_storage`→inspect_storage,
    `…host::inspect_account::<…>`→inspect_account, `foldhash::hash_bytes_long`→that."""
    for p in reversed(_split_top(demangled)):
        p = p.strip()
        if p and not p.startswith("<"):
            return p
    return demangled.strip()


def _demangle_names(symbols: list, our_token: str, binary: str) -> list:
    """Each raw v0 symbol → its function-leaf name. rustfilt (correct v0 parse: fn name
    vs its generic args) when present; the heuristic otherwise. Owner is still decided
    on the RAW symbol, so a trait method `<revm::Journal as mega_evm::Tr>::inspect_storage`
    stays OURS even though its demangled head is the revm Self-type."""
    rf = _have_rustfilt()
    if rf and symbols:
        try:
            import subprocess
            out = subprocess.run([rf], input="\n".join(symbols), capture_output=True,
                                 text=True, timeout=30)
            lines = out.stdout.splitlines()
            if out.returncode == 0 and len(lines) == len(symbols):
                return [_demangle_leaf(l) for l in lines]
        except Exception:
            pass
    return [_fn_name(s, our_token, binary) for s in symbols]


def classify_owner(symbol: str, ours):
    """(owner, why) for a (possibly mangled) symbol. owner ∈ {ours, crypto, runtime,
    unknown}. `ours` may be a single crate token (str) or the whole workspace's token
    SET — a symbol is OURS if ANY token appears in it (longest first, so a specific crate
    wins over a short one). In-crate fns are generic over external types, so a plain
    substring is enough."""
    s = symbol.lower()
    # Strip the trailing MONOMORPHIZATION-INSTANTIATION crate before the ownership check:
    # an EXTERNAL fn (e.g. arkworks `Fr::mul_assign`) monomorphized inside a workspace
    # crate carries that crate as a `Cs…_<crate>` suffix — without stripping it, every
    # arkworks op called from our code would be mis-classified `ours`.
    inst = _inst_crate(symbol)
    s_check = s.rsplit(inst.lower(), 1)[0] if inst else s
    toks = {ours} if isinstance(ours, str) else set(ours or [])
    hit = next((t for t in sorted(toks, key=len, reverse=True) if t and t in s_check), None)
    if hit:
        return "ours", hit
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
        L.append("_Attempt one with `aro run` (L2: propose → human reviews), or run the "
                 "whole list unattended with `aro sweep <spec> --attempt` (L3)._")
        L.append("| % self-time | function | next step |")
        L.append("|---|---|---|")
        for r in buckets["untried"]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | `aro run` on this hot fn, or "
                     f"`--attempt` to auto-walk the frontier |")
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

def profile_ranked(spec, top: int = 40, our_token: str = "", extra_edits=None):
    """Build the spec's profile example in an isolated worktree, sample it, and return
    `[(name, pct, symbol)]` heaviest-first over the in-binary compute frames. Empty on
    any failure (the map then reports 'no profile').

    `extra_edits` (the cumulative accepted patch) is applied to the worktree before
    building, so a re-sweep inside `--attempt` re-profiles ON TOP OF the wins so far
    (the same compounding the per-run loop does) — best-effort: a failed apply falls
    back to the base profile rather than crashing the meta-loop."""
    import subprocess
    target = SpecTarget(spec)
    work = target.make_worktree("sweep")
    try:
        b = spec.bench
        target._write_probe(work, b["pkg"], b["example"])
        if extra_edits:
            try:
                target.apply(Patch(edits=list(extra_edits)), work)
            except Exception:
                pass  # re-profile on top is best-effort; degrade to the base profile
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
    """Like profile.top_functions but KEEPS the raw symbol (for owner classification) and
    extracts a reliable leaf function name (`_fn_name`, not the weak demangler). Sampling is
    cross-platform via profile._raw_samples (macOS `sample` / Linux `perf`)."""
    from . import profile as profmod
    binary = Path(binary)
    raw = profmod.spin_and_sample(binary, spin, secs)
    rows = [(sym, cnt) for sym, image, cnt in raw
            if not any(d in image for d in profmod._DROP_IMAGES)]
    total = sum(c for _, c in rows) or 1
    rows.sort(key=lambda r: r[1], reverse=True)
    bn = Path(binary).name
    top_rows = rows[:top]
    names = _demangle_names([s for s, _ in top_rows], our_token, bn)
    return [(names[i], 100.0 * c / total, s) for i, (s, c) in enumerate(top_rows)]


# --- L3: --attempt — the unattended meta-loop ---------------------------------
#
# The map (above) is L1: report-only, no changes. `aro run` is L2: propose one
# change, a human reviews/merges. `--attempt` is L3: unattended — it walks the
# actionable frontier heaviest-first, runs the FULL per-target loop (the same
# deterministic judge: A/A floor + paired A/B + differential + auto-tighten) on
# each hot function, folds an accepted patch into the shared baseline, and
# re-profiles on top of it (compounding) until the frontier is exhausted or the
# attempt budget runs out. It writes NO new judging code — it orchestrates the
# existing `run_backtest` + `profile_ranked`.
#
# Loop-ready by construction (the four primitives a self-running loop needs):
#   budget   — `--max-attempts` caps the fan-out; `bench_scales` bounds re-benching.
#   run-log  — every attempt + every candidate verdict streams to events.jsonl.
#   gate     — architecture-gated functions are surfaced, never auto-touched; an
#              `accepted` patch is correctness+speed proven, NOT "should-merge".
#   denylist — the per-function region guard locks edits to the located source file;
#              Cargo.toml/lock, benches/, tests/ stay off-limits (the judge's rule).
#
# Comprehension debt: N unattended accepts leave N diffs a human still has to
# understand before merging. The attempt map lists exactly those diffs so the debt
# is visible, not hidden — review them; `accepted` ≠ merged.

# Verdict informativeness, best first — for picking the headline verdict of a
# per-function run from its candidates (accept is detected separately, from the
# shared pareto growing, since pareto is cumulative across functions).
_VERDICT_RANK = {"accepted": 6, "noise-limited": 5, "regressed": 4,
                 "within-noise": 3, "verify-failed": 2, "build-failed": 1, "rejected": 0}


def _grep_fn_files(src_dir: Path, name: str) -> list:
    """Files under `src_dir` (recursively) whose text defines `fn <name>`, as paths
    relative to nothing (absolute). Pure/cargo-free so it is unit-testable."""
    pat = re.compile(r"\bfn\s+" + re.escape(name) + r"\b")
    hits = []
    for rs in sorted(Path(src_dir).rglob("*.rs")):
        try:
            if pat.search(rs.read_text()):
                hits.append(rs)
        except Exception:
            continue
    return hits


def _locate_fn(target, pkg: str, name: str) -> list:
    """Repo-relative `.rs` files that define `fn <name>`, searched across ALL workspace
    member crates (Stage-1) — so a hot fn in a sibling crate (ipa-multipoint, salt) is
    locatable, not just the bench pkg. Returns paths relative to the repo root (the form
    the region guard / read-phase `context.file` expect). Empty when the name can't be
    located (a demangler artifact, a fully-inlined generic leaf, or a macro-generated fn)."""
    members = _workspace_members(target) or [pkg]
    out = []
    for member in members:
        pkg_dir = target._pkg_dir(target.repo, member)
        src = pkg_dir / "src"
        for h in _grep_fn_files(src if src.exists() else pkg_dir, name):
            try:
                out.append(str(h.relative_to(target.repo)))
            except ValueError:
                continue
    return out


def _summarize_report(report, minz: dict):
    """(headline_verdict, best_delta_pct) for one per-function run, from its OWN
    candidates (report.outcomes is per-call; report.pareto is shared/cumulative).
    Direction-aware: best Δ is the largest improvement in each metric's own direction."""
    if not report.outcomes:
        return "no-candidate", None

    best_v, best_d = None, None
    for _cand, o in report.outcomes:
        v = o.verdict.value
        if best_v is None or _VERDICT_RANK.get(v, 0) > _VERDICT_RANK.get(best_v, 0):
            best_v = v
            b = best_improvement(o.deltas, minz)
            best_d = b[0].delta_pct if b else None
    return best_v, best_d


def _seed_memory(mem_dir, cumulative_edits):
    """A FRESH per-attempt Memory pre-seeded with the cumulative accepted patch under
    UNIQUE ids (`base-0`, `base-1`, …), so run_backtest's resume re-applies the wins so
    far (correct compounding) without the live agent's reused candidate id colliding."""
    from .store import Memory
    from .types import Candidate, EvalOutcome, Patch, Verdict
    m = Memory(mem_dir)
    for j, e in enumerate(cumulative_edits):
        cid = f"base-{j}"
        m.record(Candidate(id=cid, hypothesis="", patch=Patch([e])),
                 EvalOutcome(cid, Verdict.ACCEPTED, [], []))
    return m


def _refill_queue(buckets, tries: dict, cap: int) -> list:
    """The DIVERGENT escalation: when the clean untried frontier dries, refill from
    untried+tried+gated (heaviest first), re-offering each function until it hits the
    per-fn try cap. This is what makes the search *not converge* — it keeps spending
    budget past the convergent stop point. Pure, so the policy is unit-testable."""
    cand = []
    for key in ("untried", "tried", "gated"):
        cand += [r for r in buckets.get(key, []) if isinstance(r, dict)]
    cand.sort(key=lambda r: r.get("pct", 0.0), reverse=True)
    return [r for r in cand if tries.get(r["name"], 0) < cap]


# --- the explorer's two quantities + its own continue/stop judgement -----------

def _addressable(buckets, attempted: set) -> float:
    """能进化的 — addressable HEADROOM: the self-time % sitting in our OPEN functions
    (untried + tried bucket) not yet attempted this run. By Amdahl it upper-bounds the
    additional whole-workload speedup still reachable; it shrinks monotonically as the
    explorer attempts each function (and as wins drop their share on re-profile)."""
    return sum(r["pct"] for key in ("untried", "tried")
               for r in buckets.get(key, []) if r["name"] not in attempted)


def _floor_pct(buckets) -> float:
    """碰不得的 — the untouchable floor: not-ours self-time % (crypto / runtime)."""
    return sum(r["pct"] for r in buckets.get("not_ours", []))


def _split_headroom(buckets, attempted: set, locate) -> tuple:
    """Honest headroom split: of the open (untried+tried) self-time, how much is
    ADDRESSABLE (the function source can be located → the explorer can actually attempt
    it) vs UNREACHABLE (no `fn` to edit — a demangler artifact, an inlined/closure
    frame). Only addressable counts toward the continue decision; counting unreachable
    mass as opportunity is what made the report say CONTINUE while the loop exhausted."""
    addr = unreach = 0.0
    for key in ("untried", "tried"):
        for r in buckets.get(key, []):
            if r["name"] in attempted:
                continue
            if locate(r["name"]):
                addr += r["pct"]
            else:
                unreach += r["pct"]
    return addr, unreach


def _explore_decision(headroom: float, dry_streak: int, *,
                      headroom_min: float = 2.0, dry_max: int = 3,
                      exhaustive: bool = False) -> tuple:
    """判定是否继续 — the explorer's OWN stop rule. It does not converge artificially
    (it escalates past a dry untried bucket); it stops only when the MEASURED
    opportunity is gone: headroom drained, or a run of non-accepts says the current
    power/lens can extract no more.

    `exhaustive` (token-infinite infinite-flow, §4.4): DROP the cost-saving
    `dry_streak` stop — with token not a constraint, stopping on diminishing returns
    is just leaving the tree half-walked. Then the ONLY in-decision stop is drained
    headroom; true termination comes from the loop EXHAUSTING the frontier (every
    function × lens × reflect tried up to its cap → queue + escalation empty)."""
    if headroom <= headroom_min:
        return "STOP", (f"addressable headroom {headroom:.1f}% ≤ {headroom_min:.0f}% — "
                        f"our optimizable opportunity on this workload is drained")
    if not exhaustive and dry_streak >= dry_max:
        return "STOP", (f"{dry_streak} consecutive non-accepts — diminishing returns at "
                        f"the current measurement power / lens depth")
    return "CONTINUE", (f"addressable headroom {headroom:.1f}% remains and the search is "
                        f"still landing or resolving wins" +
                        (" (exhaustive: walking the full frontier)" if exhaustive else ""))


def render_explore_report(elog, spec_name: str, profiled: str, floor_pct: float,
                          decision: str, reason: str) -> str:
    """每次探索后的报告 — what could evolve, what did, and whether to continue."""
    realized = (-elog[-1]["realized_cum"]) if elog else 0.0   # % faster (positive)
    head_now = elog[-1]["headroom"] if elog else 0.0
    unreach_now = elog[-1].get("unreachable", 0.0) if elog else 0.0
    accepts = [e for e in elog if e["accepted"]]
    L = [f"# aro explore — autoresearch report: {spec_name}", ""]
    L.append(f"_profiled `{profiled}`; step {len(elog)} of an open-ended search._")
    L.append("")
    L.append(f"- **Realized:** **{realized:.1f}% faster** cumulative "
             f"(compounded over {len(accepts)} accept(s)).")
    L.append(f"- **Addressable headroom:** **{head_now:.1f}%** of the workload "
             f"still sits in un-attempted in-crate functions we can LOCATE (Amdahl upper "
             f"bound on what more is reachable here).")
    if unreach_now > 0.5:
        L.append(f"- **Unreachable:** {unreach_now:.1f}% is in-crate but has no "
                 f"locatable `fn` (inlined / closure / a demangler artifact) — real time, "
                 f"not addressable until it can be named.")
    L.append(f"- **Untouchable floor:** ≈{floor_pct:.0f}% is not-ours (crypto / runtime) — "
             f"the asymptote this workload can't cross.")
    L.append(f"- **Decision (continue?):** **{decision}** — {reason}")
    L.append("")
    L.append("## Steps so far")
    L.append("| # | function | verdict | Δ | realized (faster) | headroom left | regime |")
    L.append("|---|---|---|---|---|---|---|")
    _regime_lab = {"relaxed": "relaxed (needs human call)", "byte-identical": "byte-identical"}
    for e in elog:
        d = f"{e['delta']:+.2f}%" if isinstance(e.get("delta"), (int, float)) else "—"
        mark = " ✅" if e["accepted"] else ""
        L.append(f"| {e['i']} | `{e['fn']}` | {e['verdict']}{mark} | {d} | "
                 f"{-e['realized_cum']:.1f}% | {e['headroom']:.1f}% | "
                 f"{_regime_lab.get(e['regime'], e['regime'])} |")
    L.append("")
    if decision == "STOP":
        L.append("> **At the limit.** The explorer stops itself: the measured headroom on "
                 "this workload is exhausted. To re-open the search, widen the workload "
                 "(a corpus that stresses other paths), climb the lens (algorithm-level), "
                 "or relax the oracle (accept should-not-merge structural wins).")
    else:
        L.append("> **More to do.** Headroom remains; the search continues to the next "
                 "function / lens.")
    L.append("")
    return "\n".join(L)


def _parent_nonregression(parent_spec, base_edits: list, new_edits: list,
                          floors, minz: dict, events, fn: str) -> bool:
    """A micro-proven win must not regress the PARENT workload before it folds:
    paired A/B on the parent bench — base (cumulative wins) vs base+new — judged
    against the parent's own A/A floors. True = safe to fold. Failure of the
    machinery itself returns False (never fold on an unverified claim)."""
    t = SpecTarget(parent_spec)
    base_w = cand_w = None
    try:
        base_w = t.make_worktree("parentchk-base")
        cand_w = t.make_worktree("parentchk-cand")
        t.apply(Patch(edits=list(base_edits)), base_w)
        t.apply(Patch(edits=list(base_edits)), cand_w)
        t.apply(Patch(edits=list(new_edits)), cand_w)
        t.build(base_w)
        t.build(cand_w)
        objs = t.objectives()
        obj_min = {o.metric: o.minimize for o in objs}
        deltas, agg = evalmod._significance(
            t, base_w, cand_w, parent_spec.ab_pairs, 1, obj_min, objs, floors)
        events.emit("parent_check", fn=fn, regressed=agg["regressed"],
                    deltas=[{"metric": d.metric, "delta_pct": round(d.delta_pct, 3)}
                            for d in deltas])
        return not agg["regressed"]
    except Exception as e:
        events.emit("parent_check", fn=fn, regressed=None, error=str(e)[:200])
        return False
    finally:
        for w in (base_w, cand_w):
            if w is not None:
                t.remove_worktree(w)


def _probe_rescue(spec, derived, fn: str, files: list, pct: float, parent_floors,
                  minz: dict, cumulative_edits: list, out_dir: Path, ran: int,
                  events, *, fanout: int, gen_concurrency: int, rounds_per_fn: int,
                  prescreen: bool, critic, per_fn_dry: int, hooks: dict):
    """L4a orchestration for ONE noise-limited node: author → qualify (frozen) →
    re-judge under the micro-bench (Gate 1 stays the PARENT oracle) → parent
    non-regression → fold. Returns (ran, row|None, new_edits). `hooks` injects
    author/bench/profile_shares/rejudge/parent_check for tests; production uses
    the real backends."""
    from . import probe_factory as pfmod
    from .engine import run_backtest
    from .generator import AgenticGenerator, RalphGenerator

    # 1) author (a separate agent call; never sees any candidate patch)
    try:
        author = hooks.get("author") or pfmod.author
        probe_rel = author(derived, fn, files)
    except Exception as e:
        events.emit("probe_author_failed", fn=fn, detail=str(e)[:200])
        return ran, None, []

    # 2) qualification gates + freeze (probe_registered)
    q = pfmod.qualify(derived, fn, probe_rel,
                      parent_floors=parent_floors, objectives=SpecTarget(derived).objectives(),
                      aa_runs=spec.aa_runs, bench=hooks.get("bench"),
                      profile_shares=hooks.get("profile_shares"), events=events)
    if not q.ok:
        return ran, None, []

    # 3) re-judge as its OWN attempt row, regime micro-proven
    micro = pfmod.micro_spec(derived, fn, probe_rel)
    ran += 1
    events.context = {"attempt": ran}
    events.emit("attempt_started", fn=fn, pct=round(pct, 2), try_n=1,
                regime="micro-proven", files=files, probe=q.sha256[:12])
    rejudge = hooks.get("rejudge")
    if rejudge is not None:
        report = rejudge(micro, ran)
    else:
        dtarget = SpecTarget(micro)
        generator = (RalphGenerator(dtarget, gen_concurrency=gen_concurrency)
                     if spec.generator == "ralph"
                     else AgenticGenerator(dtarget, gen_concurrency=gen_concurrency))
        amem = _seed_memory(out_dir / f"a{ran}", cumulative_edits)
        try:
            report = run_backtest(
                dtarget, generator, amem,
                rounds=rounds_per_fn, candidates_per_round=fanout,
                aa_runs=spec.aa_runs, ab_pairs=spec.ab_pairs,
                baseline_ref=spec.baseline_ref, events=events, goal=spec.goal,
                stop_dry_rounds=per_fn_dry, read_phase=spec.read_phase,
                bench_scales=spec.bench_scales, prescreen=prescreen, critic=critic,
                critic_context=(f"Target function `{fn}` re-judged under a QUALIFIED "
                                f"isolation micro-bench (sha {q.sha256[:12]}). Judge "
                                f"reward-hacking as usual; the probe itself is frozen."))
        except Exception as e:
            events.emit("attempt_errored", fn=fn, detail=str(e)[:200])
            return ran, {"name": fn, "pct": pct, "verdict": "errored", "delta": None,
                         "files": files, "regime": "micro-proven"}, []
    verdict, delta = _summarize_report(report, minz)

    # 4) parent non-regression before the fold (correctness is already parent-proven:
    #    the micro spec keeps the parent differential + test suite as Gate 1)
    new_edits: list = []
    if report.folded_edits:
        check = hooks.get("parent_check") or _parent_nonregression
        if check(derived, cumulative_edits, report.folded_edits, parent_floors,
                 minz, events, fn):
            new_edits = list(report.folded_edits)
        else:
            verdict = "parent-regressed"
    row = {"name": fn, "pct": pct, "verdict": verdict, "delta": delta,
           "files": files, "accepted": bool(new_edits), "regime": "micro-proven",
           "probe": q.sha256[:12]}
    events.emit("attempt_finished", fn=fn, verdict=verdict,
                delta=(round(delta, 3) if isinstance(delta, (int, float)) else None),
                accepted=bool(new_edits), regime="micro-proven")
    return ran, row, new_edits


def attempt(spec, *, max_attempts: int, rounds_per_fn: int, min_pct: float,
            top: int, out_dir: Path, events, diverge: bool = False,
            max_tries_per_fn: int = 0, fanout: int = 1, gen_concurrency: int = 8,
            exhaustive: bool = False, prescreen: bool = False,
            per_fn_dry_rounds: int = 0, critic=None,
            probe_factory: bool = False, probe_hooks: dict = None) -> tuple:
    """The L3 meta-loop. Returns `(rows, memory)` where rows are the per-function
    attempt records (for the map) and memory is the shared store carrying the
    cumulative accepted patch.

    `diverge=False` is CONVERGENT: walk the untried frontier once, stop when it
    empties (the map is the product). `diverge=True` is the INFINITE/divergent
    autoresearch policy: never stop on dry — refill from tried/gated (escalation),
    re-attempt each function up to `max_tries_per_fn`, and run until the attempt
    BUDGET (`max_attempts`) is spent. Each attempt is tagged with its oracle REGIME
    (byte-identical, or `relaxed` for an architecture-gated target where a win is
    should-not-merge) so the trajectory can draw the two kinds of win differently.

    Infinite-flow (token-infinite) knobs — design §4.1/4.2/4.3b/4.4:
      `fanout`          — candidates generated PER ROUND, in parallel, each with a
                          different lens/framing (the agent池 fan-out). >1 turns on
                          the parallel generator; 1 keeps the legacy single-candidate.
      `gen_concurrency` — cap on concurrent `claude -p` generators (generation is
                          parallel; the JUDGE stays serial — that invariant is the moat).
      `prescreen`       — cheap build+smoke gate + dedup + priority order BEFORE the
                          serial judge, so junk candidates don't hog the scarce A/A+A/B.
      `exhaustive`      — drop the cost-saving cross-fn dry-stop; walk the whole tree.
      `per_fn_dry_rounds` — per-function dry-round cap (how many reflect rounds with no
                          accept before the function is judged exhausted); 0 → spec default."""
    from .engine import run_backtest
    from .generator import AgenticGenerator, RalphGenerator

    target0 = SpecTarget(spec)
    our_token = _workspace_tokens(target0, spec.bench.get("pkg", spec.name))
    minz = {o["metric"]: o.get("minimize", True) for o in spec.objectives}
    # Driver-maintained cumulative patch — NOT a single shared Memory. The live agent
    # reuses one candidate id ("agent-r0") every attempt, which collides in a shared
    # store (the pareto SET dedups, and patches/<id>.txt gets overwritten), corrupting
    # both accept-detection and cross-attempt compounding. So each attempt gets a FRESH
    # memory seeded with `cumulative_edits` under unique ids, and an accept is detected
    # from that attempt's OWN report (not a pareto diff).
    cumulative_edits: list = []

    def reprofile():
        ranked = profile_ranked(spec, top=top, our_token=our_token,
                                extra_edits=list(cumulative_edits))
        return bucket_functions(ranked, our_token, _lesson_index(spec.name), min_pct)

    buckets = reprofile()
    queue = list(buckets["untried"])
    cap = max_tries_per_fn if max_tries_per_fn else (2 if diverge else 1)
    events.emit("attempt_frontier", untried=len(queue), policy=("diverge" if diverge
                else "converge"), budget=max_attempts, cap=cap,
                fns=[r["name"] for r in queue[:max_attempts]])
    # Untouchable floor breakdown (for the report's clickable "碰不得" view): the not-ours
    # frames (crypto / runtime) with owner + why, heaviest first.
    events.emit("profile_floor", frames=[
        {"name": r["name"], "pct": round(r["pct"], 2), "owner": r["owner"], "why": r["why"]}
        for r in buckets.get("not_ours", [])[:40]])

    tries: dict = {}
    rows: list = []
    ran = 0
    # explorer bookkeeping (diverge): compounded realized speedup, the set already
    # attempted (drives the shrinking headroom), the non-accept streak, and the
    # per-step log the running report + chart read.
    factor = 1.0
    attempted_names: set = set()
    dry_streak = 0
    elog: list = []
    floor_now = _floor_pct(buckets)
    _loc_cache: dict = {}

    def _loc(nm):
        if nm not in _loc_cache:
            _loc_cache[nm] = bool(_locate_fn(target0, spec.bench["pkg"], nm))
        return _loc_cache[nm]

    while ran < max_attempts:
        events.context = {}   # cleared between attempts; set to {"attempt": ran} below
        if not queue:
            queue = _refill_queue(buckets, tries, cap) if diverge else []
            if not queue:
                # CONVERGENT stops here (the frontier is a map); DIVERGENT only
                # reaches here when even the escalation is dry — truly nothing left.
                events.emit("attempt_exhausted", policy=("diverge" if diverge
                            else "converge"), ran=ran)
                break

        F = queue.pop(0)
        name = F["name"]
        if tries.get(name, 0) >= cap:
            continue
        gated_names = {r["name"] for r in buckets.get("gated", [])}
        regime = "relaxed" if name in gated_names else "byte-identical"

        files = _locate_fn(target0, spec.bench["pkg"], name)
        if not files:
            tries[name] = cap  # never retry an unlocatable name
            rows.append({"name": name, "pct": F["pct"], "verdict": "unlocated",
                         "delta": None, "files": [], "regime": regime})
            events.emit("attempt_skipped", fn=name, reason="source not located")
            continue

        tries[name] = tries.get(name, 0) + 1
        attempted_names.add(name)
        ran += 1
        # Stamp every event from here (attempt_started, all backtest events, the win's
        # baseline_advanced, attempt_finished) with this attempt's a<N> index, so the
        # manifest/any consumer maps an event → its attempt dir without timeline-counting.
        events.context = {"attempt": ran}
        # Retarget the WHOLE task to this function, not just the editable regions:
        # the spec's `constraints.notes` (and the original hot_path framing) would
        # otherwise steer the agent at the spec's first function and the guard then
        # rejects the out-of-region edit. Override notes + editable to name `name`.
        per_fn_constraints = dict(spec.constraints)
        per_fn_constraints["editable"] = files
        per_fn_constraints["notes"] = (
            f"Optimize the hot function `{name}` (in {files[0]}). Edit ONLY the "
            f"listed file(s) and keep behaviour byte-identical. Do NOT optimize any "
            f"other function — this attempt targets `{name}` specifically.")
        derived = dataclasses.replace(
            spec, regions=files,
            context={"file": files[0], "anchors": [["fn", name]]},
            constraints=per_fn_constraints)
        dtarget = SpecTarget(derived)
        generator = (RalphGenerator(dtarget, gen_concurrency=gen_concurrency)
                     if spec.generator == "ralph"
                     else AgenticGenerator(dtarget, gen_concurrency=gen_concurrency))

        events.emit("attempt_started", fn=name, pct=round(F["pct"], 2),
                    try_n=tries[name], regime=regime, files=files)
        amem = _seed_memory(out_dir / f"a{ran}", cumulative_edits)  # fresh, no id collision
        try:
            report = run_backtest(
                dtarget, generator, amem,
                rounds=rounds_per_fn, candidates_per_round=fanout,
                aa_runs=spec.aa_runs, ab_pairs=spec.ab_pairs,
                baseline_ref=spec.baseline_ref, events=events,
                goal=spec.goal,
                stop_dry_rounds=(per_fn_dry_rounds or spec.stop.dry_rounds),
                read_phase=spec.read_phase, bench_scales=spec.bench_scales,
                prescreen=prescreen, critic=critic,
                critic_context=(
                    f"Target function `{name}` (in {files[0]}); workload probe "
                    f"{spec.profile.get('example', spec.bench['example'])}. Implementation-source "
                    f"edits only, behaviour preserved. Judge whether this is a reward-hack, a "
                    f"gamed bench, or a known-bad pattern (e.g. PR#313 dissolving layering)."))
        except Exception as e:
            rows.append({"name": name, "pct": F["pct"], "verdict": "errored",
                         "delta": None, "files": files, "regime": regime})
            events.emit("attempt_errored", fn=name, detail=str(e)[:200])
            continue

        verdict, delta = _summarize_report(report, minz)
        # Durable cross-run lesson per candidate → a later sweep dedups this fn
        # (untried → tried) automatically, on top of the in-run try counter.
        for cand, o in report.outcomes:
            b = best_improvement(o.deltas, minz)
            lessonsmod.append(spec.name, cand.hypothesis, o.verdict.value,
                              b[0].delta_pct if b else None,
                              o.notes[-1] if o.notes else "")

        # The engine folded this attempt's round winners into its OWN baseline and reports
        # exactly those new edits as `folded_edits` (past the resumed seed). Adopt them —
        # never a per-outcome ACCEPTED that was superseded by a better sibling (it would
        # conflict on the next resume), never the seed twice. Empty on an early-errored run,
        # so a failed attempt leaves the driver's cumulative wins untouched.
        accepted_now = bool(report.folded_edits)
        cumulative_edits.extend(report.folded_edits)
        rows.append({"name": name, "pct": F["pct"], "verdict": verdict,
                     "delta": delta, "files": files, "accepted": accepted_now,
                     "regime": regime})
        events.emit("attempt_finished", fn=name, verdict=verdict,
                    delta=(round(delta, 3) if delta is not None else None),
                    accepted=accepted_now, regime=regime)

        # --- L4a: probe rescue — a noise-limited node gets an ISOLATION MICRO-BENCH
        # (authored + qualification-gated + frozen), a re-judge under it, and a
        # PARENT-workload non-regression check before its win may fold. Design
        # docs/self-extending-search-design.md §3.1; regime `micro-proven` is never
        # auto-mergeable (manifest keeps mergeable=false for non-byte-identical).
        if probe_factory and verdict == "noise-limited" and not accepted_now:
            ran, row2, new_edits = _probe_rescue(
                spec, derived, name, files, F["pct"], report.floors, minz,
                cumulative_edits, out_dir, ran, events,
                fanout=fanout, gen_concurrency=gen_concurrency,
                rounds_per_fn=rounds_per_fn, prescreen=prescreen, critic=critic,
                per_fn_dry=(per_fn_dry_rounds or spec.stop.dry_rounds),
                hooks=probe_hooks or {})
            if row2 is not None:
                rows.append(row2)
                if new_edits:
                    cumulative_edits.extend(new_edits)
                    accepted_now = True
                    verdict, delta = row2["verdict"], row2["delta"]

        if accepted_now:
            # The baseline moved → re-profile on top of all wins so far and re-bucket
            # (the ranking shifts; new functions may surface, dedup'd by the try cap).
            buckets = reprofile()
            queue = [r for r in buckets["untried"] if tries.get(r["name"], 0) < cap]
            events.emit("attempt_resweep", remaining=len(queue))

        # --- explorer step: 能进化的 / 进化了 / 判定, then write report + chart ----
        if diverge:
            if accepted_now and isinstance(delta, (int, float)):
                factor *= (1 + delta / 100.0)
                dry_streak = 0
            else:
                dry_streak += 1
            realized_cum = (factor - 1) * 100.0          # negative = faster
            headroom, unreachable = _split_headroom(buckets, attempted_names, _loc)
            floor_now = _floor_pct(buckets)
            decision, reason = _explore_decision(headroom, dry_streak,
                                                 dry_max=(per_fn_dry_rounds or 3),
                                                 exhaustive=exhaustive)
            elog.append({"i": ran, "fn": name, "verdict": verdict, "delta": delta,
                         "accepted": accepted_now, "regime": regime,
                         "realized_cum": realized_cum, "headroom": headroom,
                         "unreachable": unreachable})
            events.emit("explore_step", i=ran, fn=name, verdict=verdict,
                        realized_pct=round(-realized_cum, 2),
                        headroom_pct=round(headroom, 2), unreachable_pct=round(unreachable, 2),
                        floor_pct=round(floor_now, 1), decision=decision, reason=reason)
            # running report + chart (overwritten each step — a live dashboard)
            try:
                profiled = spec.profile.get("example", spec.bench["example"])
                (out_dir / "REPORT.md").write_text(
                    render_explore_report(elog, spec.name, profiled, floor_now,
                                          decision, reason) + "\n")
                from . import chart as _chart
                (out_dir / "trajectory.svg").write_text(
                    _chart.explore_svg(elog, floor_now, decision, reason, spec.name) + "\n")
            except Exception as e:
                events.emit("explore_report_failed", detail=str(e)[:160])
            if decision == "STOP":
                events.emit("explore_stop", i=ran, reason=reason)
                break

    return rows, cumulative_edits


def render_attempt_map(rows, spec_name: str, accepted_edits, max_attempts: int) -> str:
    """The L3 attempt report (Markdown): what was tried, the judge's verdict + Δ for
    each, the cumulative win, and the comprehension-debt note."""
    accepts = [r for r in rows if r.get("accepted")]
    files = sorted({f for r in accepts for f in r.get("files", [])})
    L = [f"# aro sweep --attempt — frontier run: {spec_name}", ""]
    L.append(f"_walked the actionable frontier heaviest-first (budget {max_attempts}); "
             f"each function ran the full judge (A/A floor + paired A/B + differential + "
             f"auto-tighten). `accepted` = correctness+speed proven, **not** should-merge._")
    L.append("")
    L.append(f"**Result:** {len(rows)} function(s) attempted · **{len(accepts)} accepted** · "
             f"{len(accepted_edits)} cumulative edit(s) across {len(files)} file(s).")
    L.append("")

    L.append("## Attempts (in order)")
    L.append("| % self-time | function | verdict | Δ | source |")
    L.append("|---|---|---|---|---|")
    for r in rows:
        d = f"{r['delta']:+.2f}%" if isinstance(r.get("delta"), (int, float)) else "—"
        mark = " ✅" if r.get("accepted") else ""
        src = "`" + "`, `".join(r["files"]) + "`" if r.get("files") else "_(unlocated)_"
        L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['verdict']}{mark} | {d} | {src} |")
    L.append("")

    if accepts:
        L.append("## Comprehension debt — review before merging")
        L.append(f"{len(accepts)} unattended accept(s) below. The judge proved each is "
                 f"correctness-preserving and a real speedup; it did **not** weigh "
                 f"architecture, readability, or whether the win is worth the change. "
                 f"That call is yours — review these diffs:")
        for r in accepts:
            d = f"{r['delta']:+.2f}%" if isinstance(r.get("delta"), (int, float)) else ""
            L.append(f"- `{r['name']}` {d} — {', '.join('`'+f+'`' for f in r['files'])}")
        L.append("")
        L.append("_The patches live under the run's `--out-dir` (`patches/`, `pareto.txt`); "
                 "`events.jsonl` is the verbatim run-log._")
    else:
        L.append("## No accept this run")
        L.append("_Every attempted function came back within-noise / noise-limited / "
                 "verify-failed at this workload's measurement power. Heaviest functions "
                 "exhaust first; a small-fraction function may need an isolation probe "
                 "(`aro plan`) or a workload that stresses it (widen the corpus)._")
    L.append("")
    return "\n".join(L)


def _svg_to_png(svg: Path, png: Path, size: int = 1400) -> bool:
    """Best-effort SVG -> PNG across platforms — macOS `qlmanage`, or `rsvg-convert` /
    `cairosvg` / `inkscape` on Linux. The SVG is the real artifact (the HTML embeds the SVG
    directly); the PNG is only a convenience for embedding in markdown. True on success."""
    import shutil
    import subprocess
    try:
        if shutil.which("qlmanage"):
            subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(png.parent), str(svg)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            produced = png.parent / (svg.name + ".png")   # qlmanage names it <file>.png
            if produced.exists():
                produced.replace(png)
                return True
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-w", str(size), "-o", str(png), str(svg)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return png.exists()
        if shutil.which("cairosvg"):
            subprocess.run(["cairosvg", str(svg), "-o", str(png), "-W", str(size)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return png.exists()
        if shutil.which("inkscape"):
            subprocess.run(["inkscape", str(svg), "--export-type=png",
                            f"--export-filename={png}", f"--export-width={size}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            return png.exists()
    except Exception:
        pass
    return False


def _finalize_run(out_dir: Path, events) -> None:
    """Closing step of an `--attempt` run (§4.5): from the verbatim events.jsonl,
    auto-build the interactive decision tree (`decision-tree.html`) and render the
    explorer's `trajectory.svg` to a `trajectory.png` (so a report can embed a PNG).
    All best-effort — a finalize failure never invalidates the run's truth (the
    events log is the source); it just means a derived artifact wasn't drawn."""
    try:
        from . import tree as _tree
        t = _tree.build_tree(out_dir)
        (out_dir / "decision-tree.html").write_text(_tree.render_html(t, t["spec"]))
        s = t["summary"]
        print(f"decision tree → {out_dir / 'decision-tree.html'} "
              f"({s['attempted']} attempted · {s['accepted']} accepted · "
              f"{s['skipped']} skipped · {s['decision']})")
        events.emit("decision_tree_written", attempted=s["attempted"],
                    accepted=s["accepted"], decision=s["decision"])
    except Exception as e:
        events.emit("decision_tree_failed", detail=str(e)[:200])

    # The hand-off artifact: the final accepted edit-set with provenance + a mergeable
    # flag, so a downstream agent turns the run into a PR by reading manifest.json
    # instead of re-deriving the timeline (aro/manifest.py).
    try:
        from . import manifest as _manifest
        m = _manifest.build_manifest(out_dir)
        (out_dir / "manifest.json").write_text(
            json.dumps(m, ensure_ascii=False, indent=1) + "\n")
        ok = sum(1 for a in m["accepted"] if a["mergeable"])
        print(f"manifest → {out_dir / 'manifest.json'} "
              f"({len(m['accepted'])} accepted · {ok} mergeable)")
    except Exception as e:
        events.emit("manifest_failed", detail=str(e)[:200])

    svg = out_dir / "trajectory.svg"
    if svg.exists() and _svg_to_png(svg, out_dir / "trajectory.png", 1000):
        print(f"trajectory chart → {out_dir / 'trajectory.png'}")

    # The headline figure: running-best speedup vs cumulative LLM output tokens (+ every
    # candidate, off-spec marks, the untouchable-floor ceiling). Built from events.jsonl.
    try:
        from . import chart as _chart
        from . import runlog
        # NOTE: deliberately unsliced (read_events, not load_run): the perf/token figure
        # spans a resumed run's whole history — compounding carries across run_ids.
        evs = runlog.read_events(out_dir)
        (out_dir / "perf-token.svg").write_text(
            _chart.perf_token_svg(evs, out_dir.name) + "\n")
        _svg_to_png(out_dir / "perf-token.svg", out_dir / "perf-token.png", 1400)
        print(f"perf chart → {out_dir / 'perf-token.svg'}")
    except Exception as e:
        events.emit("perf_chart_failed", detail=str(e)[:160])


def main(argv) -> None:
    if not argv:
        raise SystemExit("usage: python3 -m aro sweep <spec.json> "
                         "[--out report.md] [--min-pct 1.5] [--top N]\n"
                         "       python3 -m aro sweep <spec.json> --attempt "
                         "[--max-attempts N] [--rounds-per-fn N] [--out-dir DIR] [--out map.md]\n"
                         "       (infinite-flow: --diverge --fanout N --gen-concurrency N "
                         "--prescreen/--no-prescreen --exhaustive/--no-exhaustive "
                         "--probe-factory/--no-probe-factory --dry-rounds N)")

    def opt(flag, d=None):
        return argv[argv.index(flag) + 1] if flag in argv else d

    spec = specmod.load(argv[0])
    min_pct = float(opt("--min-pct", 1.5))
    top = int(opt("--top", 40))
    our_token = _workspace_tokens(SpecTarget(spec), spec.bench.get("pkg", spec.name))

    # L3: the unattended meta-loop. Walks the frontier, runs the full judge per
    # function, compounds accepts, re-profiles on top — overnight-scale; run it as
    # the foreground (harness-tracked) process, never a backgrounded subagent.
    if "--attempt" in argv:
        from .events import EventLog
        diverge = "--diverge" in argv
        # token-infinite infinite-flow defaults (design §8): the explorer (--diverge)
        # fans out per round, prescreens, walks the WHOLE frontier (exhaustive on), and
        # the budget is just a safety valve. The converge map keeps the lean single path.
        fanout = int(opt("--fanout", 3 if diverge else 1))
        gen_conc = int(opt("--gen-concurrency", 8))
        exhaustive = diverge and ("--no-exhaustive" not in argv)
        prescreen = (fanout > 1) and ("--no-prescreen" not in argv)
        # --critic turns on the SECOND judge (independent semantic reviewer) before the
        # serial deterministic judge: a reward-hack / gamed-bench / known-bad-pattern is
        # rejected (recorded + traceable) without spending the scarce serial bench.
        critic_fn = None
        if "--critic" in argv:
            from . import critic as criticmod
            critic_fn = criticmod.critique
        per_fn_dry = int(opt("--dry-rounds", 3 if diverge else 0))
        # L4a probe factory: on by default under --diverge (the infinite flow rescues
        # its noise-limited nodes), opt-in otherwise; --no-probe-factory disables.
        probe_factory = (("--probe-factory" in argv)
                         or (diverge and "--no-probe-factory" not in argv))
        max_attempts = int(opt("--max-attempts", 10000 if diverge else 6))
        rounds_per_fn = int(opt("--rounds-per-fn", 4 if diverge else 2))
        max_tries = int(opt("--max-tries-per-fn", 0))
        suffix = "-diverge" if diverge else "-attempt"
        out_dir = Path(opt("--out-dir", f"./.aro-runs/{spec.name}{suffix}"))
        out_dir.mkdir(parents=True, exist_ok=True)
        events = EventLog(out_dir / "events.jsonl", also_console=True)
        print(f"=== aro sweep --attempt{' --diverge' if diverge else ''}: {spec.name} ===")
        print(f"repo={spec.repo} baseline={spec.baseline_ref} policy="
              f"{'diverge (infinite-flow, run to exhaustion)' if diverge else 'converge (stop at map)'} "
              f"max_attempts={max_attempts} rounds_per_fn={rounds_per_fn}")
        print(f"infinite-flow: fanout={fanout} (parallel gen, cap {gen_conc}) · "
              f"prescreen={'on' if prescreen else 'off'} · "
              f"probe-factory={'on' if probe_factory else 'off'} · "
              f"critic={'on (2nd judge)' if critic_fn else 'off'} · "
              f"exhaustive={'on' if exhaustive else 'off'} · per_fn_dry={per_fn_dry or 'spec'} · "
              f"out_dir={out_dir}\nprofiling the frontier ...")
        rows, cumulative = attempt(spec, max_attempts=max_attempts,
                                   rounds_per_fn=rounds_per_fn, min_pct=min_pct, top=top,
                                   out_dir=out_dir, events=events, diverge=diverge,
                                   max_tries_per_fn=max_tries, fanout=fanout,
                                   gen_concurrency=gen_conc, exhaustive=exhaustive,
                                   prescreen=prescreen, per_fn_dry_rounds=per_fn_dry,
                                   critic=critic_fn, probe_factory=probe_factory)
        report = render_attempt_map(rows, spec.name, cumulative, max_attempts)
        out = opt("--out")
        if out:
            Path(out).write_text(report + "\n")
            print(f"attempt map → {out}")
        print("\n" + report)
        # --- closing step (§4.5): auto-generate the decision tree + chart PNG ------
        _finalize_run(out_dir, events)
        print(f"\ntruth source: {out_dir / 'events.jsonl'}  (verbatim run-log)")
        return

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
