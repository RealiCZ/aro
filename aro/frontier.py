"""frontier — the searchable map: workspace ownership, bucketing, headroom, stop rule.

Pure/cheap logic extracted from sweep.py: which crates are OURS (cargo metadata),
how ranked frames bucket into untried/tried/gated/not-ours, where a hot fn's source
lives, the explorer's addressable-headroom arithmetic and its continue/STOP rule,
and the divergent refill policy. Everything here is unit-testable without cargo
(the one cargo call — workspace metadata — is cached and fails to []).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import lessons as lessonsmod
from .symbols import _crate_token, classify_owner


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


def _lesson_index(target_name: str) -> list:
    """Relevant lessons (cross-target recall) as `[(text, verdict, gated)]`, where
    `text` is change+note lowercased and `gated` flags an architecture/maintainability
    objection — so a heavy function the judge already ruled on isn't re-queued blindly."""
    out = []
    for r in lessonsmod.recent(target_name, limit=200):
        text = ((r.get("change", "") or "") + " " + (r.get("note", "") or "")).lower()
        gated = any(w in text for w in ("architectur", "gated", "reviewer", "layer",
                                        "single-respons", "should-merge", "维护", "架构"))  # CJK terms match historic lesson data
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


def _grep_macro_files(src_dir: Path, name: str) -> list:
    """Fallback locator for MACRO-GENERATED fns (no literal `fn <name>` anywhere): find
    files where the name appears in authoring positions — as a macro's leading argument
    (`wrap_op!(name, …)` / `wrap_op!(@variant name, …)`) or as a `::name` path segment
    (dispatch-table wiring like `table[OP] = ext::name;`). Plain word matches are NOT
    counted: `.name(` method calls would false-positive on short opcode names (`pop`).
    Requires ≥2 hits (an authoring site plus its wiring) and returns `[(hits, path)]`
    best-first, so the caller can take the single strongest file."""
    pat = re.compile(r"(?:!\s*\(\s*(?:@\w+\s+)?|::\s*)" + re.escape(name) + r"\b")
    scored = []
    for rs in sorted(Path(src_dir).rglob("*.rs")):
        try:
            n = len(pat.findall(rs.read_text()))
        except Exception:
            continue
        if n >= 2:
            scored.append((n, rs))
    return sorted(scored, key=lambda t: (-t[0], str(t[1])))


def _locate_fn(target, pkg: str, name: str) -> list:
    """Repo-relative `.rs` files that define `fn <name>`, searched across ALL workspace
    member crates (Stage-1) — so a hot fn in a sibling crate (ipa-multipoint, salt) is
    locatable, not just the bench pkg. Falls back to the macro-authoring grep when no
    literal definition exists (mega-evm generates its per-opcode wrappers via
    `wrap_op_compute_gas!(push1, …)`; the macro body IS the lever, and one edit there
    improves every wrapped opcode). Returns paths relative to the repo root (the form
    the region guard / read-phase `context.file` expect). Empty when the name can't be
    located (a demangler artifact, a fully-inlined generic leaf, or an external fn that
    ownership classification mislabeled as ours)."""
    members = _workspace_members(target) or [pkg]
    out, macro_hits = [], []
    for member in members:
        pkg_dir = target.pkg_dir(target.repo, member)
        src = pkg_dir / "src"
        root = src if src.exists() else pkg_dir
        for h in _grep_fn_files(root, name):
            try:
                out.append(str(h.relative_to(target.repo)))
            except ValueError:
                continue
        if not out:
            macro_hits.extend(_grep_macro_files(root, name))
    if out:
        return out
    # Macro fallback: take only the single strongest file — authoring sites concentrate
    # (invocation + table wiring in one module), and a wide net would balloon the
    # per-attempt editable region on generic names.
    for _, h in sorted(macro_hits, key=lambda t: (-t[0], str(t[1])))[:1]:
        try:
            return [str(h.relative_to(target.repo))]
        except ValueError:
            continue
    return []


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
    """Addressable HEADROOM: the self-time % sitting in our OPEN functions
    (untried + tried bucket) not yet attempted this run. By Amdahl it upper-bounds the
    additional whole-workload speedup still reachable; it shrinks monotonically as the
    explorer attempts each function (and as wins drop their share on re-profile)."""
    return sum(r["pct"] for key in ("untried", "tried")
               for r in buckets.get(key, []) if r["name"] not in attempted)


def _floor_pct(buckets) -> float:
    """The untouchable floor: not-ours self-time % (crypto / runtime)."""
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
    """Continue or stop: the explorer's OWN stop rule. It does not converge artificially
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

