"""aro tree — turn a run's events into the data the search-map front-end renders.

`build_tree` reads an explorer run's `out-dir` (events.jsonl + a{N}/records.jsonl +
a{N}/patches/) into a plain dict (`tree.json`): the run summary + the runtime-coverage
decomposition + every attempted function with its candidates (compact diffs), verdicts,
and the reflect-proposed-but-UNTRIED branches.

Rendering is NOT done here — it lives in the dedicated front-end under `viz/` (Svelte +
Vite + d3-hierarchy), built to a single self-contained `decision_tree_template.html`.
`render_html` only injects the data into that template's `<!--ARO_DATA-->` placeholder,
so Python authors no HTML/JS. The product (coverage bar + horizontal icicle + clickable
detail panel) is one standalone .html — no CDN, no deps.

    python3 -m aro tree <out-dir> [--out tree.html]   # writes tree.json + decision-tree.html
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path


def _latest_slice(evs):
    rids = [e.get("run_id") for e in evs if e.get("run_id")]
    if not rids:
        return evs
    last = rids[-1]
    return [e for e in evs if e.get("run_id") == last]


def _compact_diff(patch_text: str) -> str:
    """A candidate's stored patch is whole-file SEARCH→REPLACE blocks (huge). Turn it
    into a COMPACT unified diff — only the changed hunks with 3 lines of context, `+`/`-`
    prefixed — so the report shows the actual edit, not the whole file. Per edit, a
    `# <path>` header then the `@@`/`+`/`-`/` ` lines (the `---`/`+++` file headers dropped)."""
    from . import store
    try:
        edits = store._parse_patch_file(patch_text)
    except Exception:
        return patch_text
    if not edits:
        return ""
    out = []
    for e in edits:
        ud = list(difflib.unified_diff(e.search.splitlines(), e.replace.splitlines(),
                                       lineterm="", n=3))
        body = "\n".join(l for l in ud if not l.startswith("--- ") and not l.startswith("+++ "))
        out.append(f"# {e.path}\n{body}" if body.strip() else f"# {e.path}\n(无文本差异)")
    return "\n\n".join(out)


def build_tree(out_dir) -> dict:
    out_dir = Path(out_dir)
    evs = []
    for ln in (out_dir / "events.jsonl").read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                evs.append(json.loads(ln))
            except Exception:
                pass
    evs = _latest_slice(evs)

    nodes, steps = [], {}
    cur = None
    ran = 0
    frontier = []
    floor_frames = []
    for e in evs:
        ev = e.get("event")
        if ev == "attempt_frontier":
            frontier = e.get("fns", []) or []
        elif ev == "profile_floor":
            floor_frames = e.get("frames", []) or []
        elif ev == "attempt_started":
            ran += 1
            cur = {"id": f"a{ran}", "type": "fn", "i": ran, "fn": e.get("fn"),
                   "regime": e.get("regime"), "pct": e.get("pct"),
                   "files": e.get("files", []), "reflect": [], "candidates": [],
                   "status": "running"}
            nodes.append(cur)
        elif ev == "attempt_skipped":
            nodes.append({"id": f"sk{len(nodes)}", "type": "skipped",
                          "fn": e.get("fn"), "reason": e.get("reason")})
        elif ev == "direction_proposed" and cur is not None:
            cur["reflect"].append({"id": e.get("id"), "text": e.get("direction"),
                                   "tried": False})
        elif ev == "critic" and cur is not None:
            # the SECOND judge's verdict on a candidate (by id) — kept per-attempt so it
            # attaches to the right candidate when its record is loaded below.
            cur.setdefault("_critic", {})[e.get("id")] = {
                "verdict": e.get("verdict"), "reasons": e.get("reasons", [])}
        elif ev == "attempt_finished" and cur is not None:
            cur["status"] = e.get("verdict")
            cur["delta"] = e.get("delta")
            cur["accepted"] = e.get("accepted")
            cur["regime"] = e.get("regime")
            cur = None
        elif ev == "explore_step":
            steps[e.get("i")] = e

    # attach the explorer's per-step decision; load each attempt's candidate(s) + diff
    for n in nodes:
        if n["type"] != "fn":
            continue
        s = steps.get(n["i"], {})
        n["decision"] = s.get("decision")
        n["reason"] = s.get("reason")
        n["realized"] = s.get("realized_pct")
        n["headroom"] = s.get("headroom_pct")
        rec = out_dir / n["id"] / "records.jsonl"
        if rec.exists():
            for ln in rec.read_text().splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if str(r.get("id", "")).startswith("base-"):
                    continue  # cumulative-compound seed, not a real candidate
                diff_p = out_dir / n["id"] / "patches" / (r["id"] + ".txt")
                n["candidates"].append({
                    "id": r["id"], "hypothesis": r.get("hypothesis", ""),
                    "verdict": r.get("verdict"), "metrics": r.get("metrics", []),
                    "notes": r.get("notes", []),
                    "critic": n.get("_critic", {}).get(r["id"]),  # the 2nd judge's verdict+reasons
                    "diff": _compact_diff(diff_p.read_text()) if diff_p.exists() else ""})
        n.pop("_critic", None)  # temp index — drop from the emitted tree

    last_step = steps.get(max(steps) if steps else None, {})
    attempted = [n for n in nodes if n["type"] == "fn"]
    floor = round(last_step.get("floor_pct", 0.0) or 0.0, 1)
    unreach = round(last_step.get("unreachable_pct", 0.0) or 0.0, 1)
    head = round(last_step.get("headroom_pct", 0.0) or 0.0, 1)
    # Coverage decomposition (self-time %), one entry per distinct FUNCTION (the two
    # sstore attempts are the same frame → dedup, accepted status wins). The bar shows
    # WHERE the runtime is + WHAT we did to each part; the realized speedup is separate.
    best = {}
    for n in attempted:
        nm = n.get("fn"); st = n.get("status"); pct = n.get("pct") or 0.0
        if nm not in best or st == "accepted" or n.get("accepted"):
            best[nm] = ("accepted" if n.get("accepted") else st, pct)
    cap = round(sum(p for s, p in best.values() if s == "accepted"), 1)
    tried_fail = round(sum(p for s, p in best.values() if s != "accepted"), 1)
    segs = [
        {"key": "captured", "label": "已优化(accept)", "pct": cap, "color": "#16a34a"},
        {"key": "tried", "label": "试过没过", "pct": tried_fail, "color": "#cbd5e1"},
        {"key": "headroom", "label": "未试(headroom)", "pct": head, "color": "#93c5fd"},
        {"key": "unreachable", "label": "够不着(内联/宏)", "pct": unreach, "color": "#e5e7eb", "hatch": True},
        {"key": "floor", "label": "碰不得(crypto/runtime)", "pct": floor, "color": "#475569"},
    ]
    rest = round(max(0.0, 100.0 - sum(s["pct"] for s in segs)), 1)
    if rest >= 0.5:
        segs.append({"key": "other", "label": "其它/未归类", "pct": rest, "color": "#f1f5f9"})
    summary = {
        "attempted": len(attempted),
        "accepted": sum(1 for n in attempted if n.get("accepted")),
        "skipped": sum(1 for n in nodes if n["type"] == "skipped"),
        "realized_pct": last_step.get("realized_pct", 0.0),
        "headroom_pct": head, "floor_pct": floor, "unreachable_pct": unreach,
        "decision": last_step.get("decision", "?"),
        "reason": last_step.get("reason", ""),
        "frontier": frontier, "coverage": segs, "floor_frames": floor_frames,
    }
    return {"spec": out_dir.name, "summary": summary, "nodes": nodes}


_TEMPLATE_PATH = Path(__file__).parent / "decision_tree_template.html"


def render_html(tree: dict, title: str = "") -> str:
    """Inject the run's data into the prebuilt single-file front-end (the Svelte app
    under `viz/`, built to `decision_tree_template.html`). Python authors NO HTML/JS — it
    only swaps the `<!--ARO_DATA-->` placeholder for a script setting window.__ARO_DATA__.
    `title` is accepted for back-compat; the front-end derives its title from the data."""
    data = json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE_PATH.read_text().replace(
        "<!--ARO_DATA-->", f"<script>window.__ARO_DATA__ = {data};</script>")


def main(argv) -> None:
    if not argv:
        raise SystemExit("usage: python3 -m aro tree <out-dir> [--out tree.html]")
    out_dir = argv[0]
    tree = build_tree(out_dir)
    # The machine-readable data the front-end consumes (Python's only product now).
    Path(out_dir).joinpath("tree.json").write_text(
        json.dumps(tree, ensure_ascii=False, indent=1))
    html = render_html(tree)
    out = (argv[argv.index("--out") + 1] if "--out" in argv
           else str(Path(out_dir) / "decision-tree.html"))
    Path(out).write_text(html)
    print(f"decision tree → {out}")
    print(f"  data → {Path(out_dir) / 'tree.json'}")
    print(f"  {tree['summary']['attempted']} attempted · "
          f"{tree['summary']['accepted']} accepted · "
          f"{tree['summary']['skipped']} skipped · {tree['summary']['decision']}")


if __name__ == "__main__":
    main(sys.argv[1:])
