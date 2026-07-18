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
from .symbols import _crate_token, _symbol_crate_tokens, classify_owner


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


def _lesson_fresh(repo, baseline_sha, files, head_ref: str = "HEAD") -> bool:
    """True only when we can prove `files` are unchanged since `baseline_sha`.

    Mirrors recheck.assess's region-churn idea at fn-file granularity: if the
    file moved since the lesson was learned, the verdict is about code that no
    longer exists as judged. Missing inputs / git failure → False (fail-closed
    against suppression; fail-open toward exploration).
    """
    if not repo or not baseline_sha or not files:
        return False
    try:
        from . import vcs
        base = vcs.rev_parse(repo, baseline_sha)
        head = vcs.rev_parse(repo, head_ref)
        if not base or not head:
            return False
        if base == head:
            return True
        out = vcs.git(repo, "diff", "--name-only", base, head, "--", *list(files))
        if out.returncode != 0:
            return False
        return not (out.stdout or "").strip()
    except Exception:
        return False


def _lesson_index(target_name: str, repo=None) -> list:
    """Relevant lessons as `[(text, verdict, gated, meta)]`.

    `text` is change+note lowercased; `gated` flags an architecture/maintainability
    objection. `meta` carries suppression inputs:
      {"source", "same_target", "baseline_sha"}

    Recall uses lessons.recent (exact target + same-repo + global) — name-token
    fuzzy overlap is gone. Whether a match may place a fn in tried/gated is
    decided later by `bucket_functions` (same-target + stamped + fresh).

    `gated` prefers a STRUCTURED `gated` field when the row carries one; the keyword
    fallback (historic freeform rows) is deliberately narrow. It once included bare
    "layer"/"gated"/"reviewer", which poisoned the ledger the moment campaign notes
    started quoting critic audits: a note saying "layer-PRESERVING macro arm" (asserting
    layer safety!) or "gated the rex5 check behind X" (conditional-gating verb) flagged
    the whole FUNCTION as architecture-gated, silently rerouting its future wins into
    the never-mergeable relaxed regime."""
    out = []
    for r in lessonsmod.recent(target_name, limit=200, repo=repo):
        text = ((r.get("change", "") or "") + " " + (r.get("note", "") or "")).lower()
        if "gated" in r:
            gated = bool(r.get("gated"))
        else:
            gated = any(w in text for w in ("architectur", "scope-limit", "should-merge",
                                            "single-respons"))
        src = r.get("target", "") or ""
        meta = {
            "source": src,
            "same_target": src == target_name,
            "baseline_sha": r.get("baseline_sha") or None,
        }
        out.append((text, r.get("verdict", ""), gated, meta))
    return out


def _parse_lesson_entry(entry):
    """Normalize a lessons_idx entry to (text, verdict, gated, meta|None).

    3-tuples (legacy tests / direct callers) keep prior suppress-on-match
    behaviour via meta=None. Production `_lesson_index` always emits 4-tuples.
    """
    if isinstance(entry, dict):
        return (entry.get("text", ""), entry.get("verdict", ""),
                bool(entry.get("gated")), entry.get("meta"))
    if len(entry) >= 4:
        return entry[0], entry[1], entry[2], entry[3]
    return entry[0], entry[1], entry[2], None


def _suppress_ok(meta, files, repo, head_ref, fresh_check) -> tuple:
    """(may_suppress, downgrade_reason|None). meta is None → force suppress (3-tuple)."""
    if meta is None:
        return True, None
    if not meta.get("same_target"):
        return False, "cross-target"
    sha = meta.get("baseline_sha")
    if not sha:
        return False, "unstamped"
    checker = fresh_check if fresh_check is not None else _lesson_fresh
    if not checker(repo, sha, files, head_ref):
        return False, "stale"
    return True, None


# Leaf names that are library / generic methods (a demangler collapse of many distinct
# monomorphizations), not a mega-evm-specific lever — aggregated, not listed as actionable.
_GENERIC_LEAVES = {
    "convert", "error", "fast", "get", "get_mut", "insert", "remove", "contains_key",
    "rustc_entry", "entry", "eq", "cmp", "clone", "hash", "fmt", "from", "into",
    "default", "drop", "fold", "next", "index", "deref", "len", "is_empty", "as_ref",
    "reserve", "grow", "extend", "collect", "iter", "map", "unwrap", "expect"}


def bucket_functions(ranked, our_token: str, lessons_idx: list, min_pct: float,
                     classify: dict = None, *, repo=None, head_ref: str = "HEAD",
                     locate=None, fresh_check=None):
    """Classify the ranked (name, pct, symbol) frames. Aggregates by leaf name (distinct
    monomorphizations of the same function sum up), splits library/generic leaves off as
    a single tally (not actionable domain levers), and classifies the rest of OUR
    functions against the cross-run lessons. Returns a dict of bucket → [rows].

    Tried/gated suppression requires strong evidence (T51 polarity):
      same target (exact name) + baseline_sha stamped + file still fresh.
    Everything else that name-matches a lesson stays untried and is recorded in
    `lesson_downgraded` (reason: cross-target | stale | unstamped). 3-tuple
    lessons_idx entries (tests / direct callers) remain suppress-on-match.
    """
    ours_dom, ours_gen, notours, ours_sym = {}, 0.0, {}, {}
    for name, pct, symbol in ranked:
        if pct < min_pct:
            continue
        owner, why = classify_owner(symbol, our_token, extra=classify)
        if owner != "ours":
            notours.setdefault((name, owner, why), 0.0)
            notours[(name, owner, why)] += pct
            continue
        if name in _GENERIC_LEAVES:
            ours_gen += pct
        else:
            ours_dom[name] = ours_dom.get(name, 0.0) + pct
            # Keep the HEAVIEST frame's raw symbol per leaf: it names the defining
            # crate, which _locate_fn uses to break same-name collisions across
            # workspace members.
            if pct >= ours_sym.get(name, (0.0, ""))[0]:
                ours_sym[name] = (pct, symbol)

    parsed = [_parse_lesson_entry(e) for e in (lessons_idx or [])]
    buckets = {"untried": [], "tried": [], "gated": [], "not_ours": [],
               "generic_pct": ours_gen, "lesson_downgraded": []}
    for name, pct in sorted(ours_dom.items(), key=lambda kv: kv[1], reverse=True):
        sym = ours_sym.get(name, (0.0, ""))[1]
        # WORD-BOUNDARY match: bare substring matching made `add` inherit every lesson
        # containing "added" (78 false matches on real data) and `call` every "calls".
        pat = re.compile(r"(?<![a-z0-9_])" + re.escape(name.lower()) + r"(?![a-z0-9_])") \
            if name else None
        files = []
        if locate is not None:
            try:
                files = list(locate(name, sym) or [])
            except TypeError:
                try:
                    files = list(locate(name) or [])
                except Exception:
                    files = []
            except Exception:
                files = []

        suppress_hits = []   # (verdict, gated)
        for text, verdict, gated, meta in parsed:
            if not (pat and pat.search(text)):
                continue
            ok, reason = _suppress_ok(meta, files, repo, head_ref, fresh_check)
            if ok:
                suppress_hits.append((verdict, gated))
            else:
                # Would have bucketed under the pre-T51 name-match rule.
                buckets["lesson_downgraded"].append({
                    "fn": name,
                    "source": (meta or {}).get("source", "") if meta else "",
                    "reason": reason or "unstamped",
                })

        if any(g for _, g in suppress_hits):
            buckets["gated"].append({"name": name, "pct": pct, "symbol": sym,
                                     "verdict": next(v for v, g in suppress_hits if g)})
        elif suppress_hits:
            buckets["tried"].append({"name": name, "pct": pct, "symbol": sym,
                                     "verdict": suppress_hits[-1][0]})
        else:
            buckets["untried"].append({"name": name, "pct": pct, "symbol": sym})
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


def _owner_member(members: list, symbol: str):
    """The workspace member whose crate token appears in `symbol`'s DEFINING path
    (the trailing monomorphization-instantiation crate stripped first, so a generic
    fn instantiated by the probe binary doesn't match the probe). Longest token wins
    (`mega-evm-core` beats `mega-evm`). None when no member token matches — then the
    caller keeps every match (no false precision)."""
    from .symbols import _inst_crate
    s = symbol or ""
    inst = _inst_crate(s)
    if inst:
        s = s.rsplit(inst, 1)[0]
    best = None
    for m in sorted(members, key=lambda m: -len(m)):
        if _crate_token(m) and _crate_token(m) in s:
            best = m
            break
    return best


def _locate_fn(target, pkg: str, name: str, symbol: str = "") -> list:
    """Repo-relative `.rs` files that define `fn <name>`, searched across ALL workspace
    member crates (Stage-1) — so a hot fn in a sibling crate (ipa-multipoint, salt) is
    locatable, not just the bench pkg. When the profiled SYMBOL is supplied, a
    same-name collision across members is broken by the symbol's defining crate
    (`fn execute` in three crates → only the one the profiler actually saw), keeping
    the per-attempt editable region tight. Falls back to the macro-authoring grep when
    no literal definition exists (mega-evm generates its per-opcode wrappers via
    `wrap_op_compute_gas!(push1, …)`; the macro body IS the lever, and one edit there
    improves every wrapped opcode). Returns paths relative to the repo root (the form
    the region guard / read-phase `context.file` expect). Empty when the name can't be
    located (a demangler artifact, a fully-inlined generic leaf, or an external fn that
    ownership classification mislabeled as ours)."""
    members = _workspace_members(target) or [pkg]
    owner = _owner_member(members, symbol) if symbol else None
    if owner:
        members = [owner] + [m for m in members if m != owner]
    out, macro_hits = [], []
    for member in members:
        pkg_dir = target.pkg_dir(target.repo, member)
        src = pkg_dir / "src"
        root = src if src.exists() else pkg_dir
        hits = []
        for h in _grep_fn_files(root, name):
            try:
                hits.append(str(h.relative_to(target.repo)))
            except ValueError:
                continue
        if hits and member == owner:
            return hits          # defining crate resolved: ignore same-name twins
        out.extend(hits)
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


def _search_roots_ready(target, pkg: str) -> bool:
    """True when at least one locate search root exists on disk — without a
    root the miss is ambiguous (tooling/layout), not a scope verdict."""
    members = _workspace_members(target) or ([pkg] if pkg else [])
    if not members:
        return False
    for member in members:
        try:
            pkg_dir = target.pkg_dir(target.repo, member)
        except Exception:
            continue
        src = pkg_dir / "src"
        root = src if src.exists() else pkg_dir
        if root.exists():
            return True
    return False


def _classify_locate_miss(target, pkg: str, name: str, symbol: str = "",
                          regions=None) -> str:
    """When ``_locate_fn`` returned [], decide ``out-of-scope-external`` vs
    plain ``unlocated``.

    Immediate ``out-of-scope-external`` requires POSITIVE foreign evidence: the
    symbol yields at least one crate-path token and NONE of those tokens is a
    target-workspace member (e.g. ``revm`` / ``alloy_*`` when members are
    ``mega_evm*``). A single clean miss without that evidence must not close —
    macro-generated wrappers and demangler artifacts look the same as external
    crates to the locator, and a false close is irreversible for the frontier.

    Everything else stays ``unlocated`` (re-pollable): target-crate-token miss,
    tokenless miss, roots unavailable. The attempt driver closes on the 3rd
    plain-unlocated record across runs (``unlocated 3x — treated as external``).
    """
    del name, regions
    tokens = _symbol_crate_tokens(symbol or "")
    if not tokens:
        return "unlocated"
    ours = {t for t in _workspace_tokens(target, pkg) if t}
    if any(t in ours for t in tokens):
        return "unlocated"  # target-crate token present — patience via 3× counter
    return "out-of-scope-external"  # only foreign crate tokens → positive evidence


def _closed_out_of_scope(ledger_rows, workload: str = None) -> set:
    """Fn names whose LATEST observation is ``out-of-scope-external``.

    Candidate-level closed set — the frontier must never re-poll these.
    When ``workload`` is set, only that lane counts; otherwise any lane."""
    latest: dict = {}
    for r in ledger_rows:
        fn = r.get("fn")
        if not fn:
            continue
        if workload is not None and r.get("workload") != workload:
            continue
        latest[fn] = r.get("verdict")
    return {fn for fn, v in latest.items() if v == "out-of-scope-external"}


def _unlocated_count(ledger_rows, fn: str, workload: str = None) -> int:
    """How many times ``fn`` was recorded as plain ``unlocated`` (attempt counter)."""
    n = 0
    for r in ledger_rows:
        if r.get("fn") != fn:
            continue
        if workload is not None and r.get("workload") != workload:
            continue
        if r.get("verdict") == "unlocated":
            n += 1
    return n


def _pending_names(ledger_rows, workload: str) -> set:
    """Open debts from the permanent ledger for `workload`: fns whose LATEST
    observation is unresolved — noise-limited (measurement debt), no-attempt
    (frontier residue), or no-candidate (a NON-judgment: zero candidates ever
    reached the judge, e.g. the generation agent was quota-dead). These seed
    the next walk ahead of the fresh frontier, so a resumed campaign pays its
    debts before exploring. Keep in sync with permtree.open_debts."""
    latest: dict = {}
    for r in ledger_rows:
        if r.get("workload") == workload and r.get("fn"):
            latest[r["fn"]] = r.get("verdict")
    return {fn for fn, v in latest.items()
            if v in ("noise-limited", "no-attempt", "no-candidate")}


def _promote_pending(buckets, pending: set, tries: dict, cap: int) -> list:
    """Queue order for the walk: ledger debts FIRST (heaviest first, pulled from
    any open bucket — a noise-limited fn sits in `tried`), then the fresh untried
    frontier. Only fns on the CURRENT profile are promoted: a debt that fell off
    the profile is no longer addressable mass and re-attempting it would judge a
    function the workload no longer exercises."""
    rows = [r for key in ("untried", "tried", "gated") for r in buckets.get(key, [])]
    front = sorted((r for r in rows if r["name"] in pending
                    and tries.get(r["name"], 0) < cap),
                   key=lambda r: -r.get("pct", 0.0))
    names = {r["name"] for r in front}
    rest = [r for r in buckets.get("untried", [])
            if r["name"] not in names and tries.get(r["name"], 0) < cap]
    return front + rest


def apply_seed_bias(queue: list, seed_fns: list) -> tuple:
    """Bias frontier attempt order: seeded fns first (stable among seeds).

    Pure ordering bias only — no gate/judge/acceptance changes. A seed whose
    ``fn`` is not on the current queue is reported in ``skipped`` (caller emits
    ``seed_skipped`` events). Returns ``(new_queue, applied, skipped)`` where
    ``applied`` / ``skipped`` are ordered lists of seed fn names.
    """
    if not seed_fns:
        return list(queue), [], []
    # Preserve seed order; first occurrence wins for duplicate fns.
    ordered_seed_fns: list = []
    seen_seed: set = set()
    for fn in seed_fns:
        name = str(fn or "").strip()
        if not name or name in seen_seed:
            continue
        seen_seed.add(name)
        ordered_seed_fns.append(name)

    by_name = {}
    for r in queue:
        if isinstance(r, dict) and r.get("name") is not None:
            by_name.setdefault(r["name"], r)

    applied: list = []
    skipped: list = []
    front: list = []
    for name in ordered_seed_fns:
        row = by_name.get(name)
        if row is None:
            skipped.append(name)
            continue
        applied.append(name)
        front.append(row)

    front_names = set(applied)
    rest = [r for r in queue
            if not (isinstance(r, dict) and r.get("name") in front_names)]
    return front + rest, applied, skipped


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

