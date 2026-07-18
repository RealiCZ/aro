"""report_md — the human-readable Markdown renderers (map / explore / attempt).

Rendering only: every number comes verbatim from the caller (which read it from
events.jsonl or the live buckets); verdicts are never re-judged here.
"""
from __future__ import annotations


def render_map(buckets, spec_name: str, profiled: str, min_pct: float) -> str:
    """The frontier-map report (Markdown)."""
    L = [f"# aro sweep frontier map: {spec_name}", ""]
    L.append(f"_profiled `{profiled}`; in-crate functions ≥ {min_pct:.1f}% self-time._")
    L.append("")

    own = sum(r["pct"] for b in ("untried", "tried", "gated") for r in buckets[b])
    gen = buckets.get("generic_pct", 0.0)
    notours = sum(r["pct"] for r in buckets["not_ours"])
    L.append(f"**Where the time goes (of the ranked frames):** our named functions ≈ "
             f"{own:.0f}% · our generic/library work ≈ {gen:.0f}% (monomorphized "
             f"conversions / map ops: diffuse, not a clean lever) · not-ours ≈ "
             f"{notours:.0f}% (crypto / runtime, untouchable).")
    L.append("")

    L.append("## Actionable frontier: untried in-crate functions (heaviest first)")
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
                 "attempted; the clean frontier is exhausted (see below)._")
    L.append("")

    if buckets["tried"]:
        L.append("## Already attempted (the judge ruled)")
        L.append("| % | function | verdict |")
        L.append("|---|---|---|")
        for r in buckets["tried"]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['verdict']} |")
        L.append("")

    if buckets["gated"]:
        L.append("## Blocked: needs a human call (architecture / maintainability)")
        L.append("| % | function | why |")
        L.append("|---|---|---|")
        for r in buckets["gated"]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['verdict']}: a recorded "
                     f"structural / reviewer objection; `accepted` ≠ should-merge |")
        L.append("")

    if buckets["not_ours"]:
        L.append("## Not our lever (untouchable / external)")
        L.append("| % | frame | owner |")
        L.append("|---|---|---|")
        for r in buckets["not_ours"][:12]:
            L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['owner']} ({r['why']}) |")
        L.append("")

    downgraded = buckets.get("lesson_downgraded") or []
    if downgraded:
        L.append("## Lesson downgrades (informational only — not suppressing frontier)")
        L.append("_Name-matched lessons that would have filled the tried bucket under the "
                 "old rule, but failed the strong-evidence gate (cross-target / stale / "
                 "unstamped). They still inform the generator prompt._")
        L.append("| function | lesson source | reason |")
        L.append("|---|---|---|")
        seen = set()
        for d in downgraded:
            key = (d.get("fn"), d.get("source"), d.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            L.append(f"| `{d.get('fn', '')}` | `{d.get('source', '')}` | "
                     f"{d.get('reason', '')} |")
        L.append("")

    if not buckets["untried"]:
        L.append("## Converged: what unblocks the next gain")
        L.append("- **Widen the workload**: a different / broader corpus exposes different "
                 "hot paths; re-run the sweep on it.")
        L.append("- **Climb the lens**: micro-elimination → data-layout → algorithm → a "
                 "structurally-clean cross-cutting refactor (the higher tiers open new space).")
        L.append("- **A human call** on any architecture-gated item above.")
        L.append("")
    return "\n".join(L)



def render_explore_report(elog, spec_name: str, profiled: str, floor_pct: float,
                          decision: str, reason: str) -> str:
    """The per-step explorer report: what could evolve, what did, and whether to continue."""
    realized = (-elog[-1]["realized_cum"]) if elog else 0.0   # % faster (positive)
    head_now = elog[-1]["headroom"] if elog else 0.0
    unreach_now = elog[-1].get("unreachable", 0.0) if elog else 0.0
    accepts = [e for e in elog if e["accepted"]]
    L = [f"# aro explore, autoresearch report: {spec_name}", ""]
    L.append(f"_profiled `{profiled}`; step {len(elog)} of an open-ended search._")
    L.append("")
    L.append(f"- **Realized:** **{realized:.1f}% faster** cumulative "
             f"(compounded over {len(accepts)} accept(s)).")
    L.append(f"- **Addressable headroom:** **{head_now:.1f}%** of the workload "
             f"still sits in un-attempted in-crate functions we can LOCATE (Amdahl upper "
             f"bound on what more is reachable here).")
    if unreach_now > 0.5:
        L.append(f"- **Unreachable:** {unreach_now:.1f}% is in-crate but has no "
                 f"locatable `fn` (inlined / closure / a demangler artifact): real time, "
                 f"not addressable until it can be named.")
    L.append(f"- **Untouchable floor:** ≈{floor_pct:.0f}% is not-ours (crypto / runtime), "
             f"the asymptote this workload can't cross.")
    L.append(f"- **Decision (continue?):** **{decision}**: {reason}")
    L.append("")
    L.append("## Steps so far")
    L.append("| # | function | verdict | Δ | realized (faster) | headroom left | regime |")
    L.append("|---|---|---|---|---|---|---|")
    _regime_lab = {"relaxed": "relaxed (needs human call)", "byte-identical": "byte-identical"}
    for e in elog:
        d = f"{e['delta']:+.2f}%" if isinstance(e.get("delta"), (int, float)) else "-"
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



def render_attempt_map(rows, spec_name: str, accepted_edits, max_attempts: int) -> str:
    """The L3 attempt report (Markdown): what was tried, the judge's verdict + Δ for
    each, the cumulative win, and the comprehension-debt note."""
    accepts = [r for r in rows if r.get("accepted")]
    files = sorted({f for r in accepts for f in r.get("files", [])})
    L = [f"# aro sweep --attempt frontier run: {spec_name}", ""]
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
        d = f"{r['delta']:+.2f}%" if isinstance(r.get("delta"), (int, float)) else "-"
        mark = " ✅" if r.get("accepted") else ""
        src = "`" + "`, `".join(r["files"]) + "`" if r.get("files") else "_(unlocated)_"
        L.append(f"| {r['pct']:.1f}% | `{r['name']}` | {r['verdict']}{mark} | {d} | {src} |")
    L.append("")

    if accepts:
        L.append("## Comprehension debt: review before merging")
        L.append(f"{len(accepts)} unattended accept(s) below. The judge proved each is "
                 f"correctness-preserving and a real speedup; it did **not** weigh "
                 f"architecture, readability, or whether the win is worth the change. "
                 f"That call is yours. Review these diffs:")
        for r in accepts:
            d = f"{r['delta']:+.2f}%" if isinstance(r.get("delta"), (int, float)) else ""
            L.append(f"- `{r['name']}` {d}: {', '.join('`'+f+'`' for f in r['files'])}")
        L.append("")
        L.append("_The patches live under the run's `--out-dir` (`patches/`, `pareto.txt`); "
                 "`events.jsonl` is the verbatim run-log._")
    else:
        L.append("## No accept this run")
        L.append("_Every attempted function came back within-noise / noise-limited / "
                 "verify-failed at this workload's measurement power. Heaviest functions "
                 "exhaust first; a small-fraction function may need an isolation probe "
                 "(`aro init` + probes) or a workload that stresses it (widen the corpus)._")
    L.append("")
    return "\n".join(L)

