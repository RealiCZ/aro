"""`aro union` — the cross-campaign view over permanent-tree ledgers.

One page over ANY number of `memory/permtree/<spec>.jsonl` ledgers: workloads as
lanes, every function's judgment side by side across lanes, per-lane banked wins,
and the global open measurement debt. This is the "multi-workload permanent
decision tree" panorama from the self-extending-search design: a single run's
report shows one campaign; the union shows what the whole PROGRAM of campaigns
has proven, and what is still owed.

Read-only and derived: it renders ledger records verbatim (every row keeps its
`events` pointer back to the producing run dir), never re-judges anything.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import permtree

_TEMPLATE_PATH = Path(__file__).parent / "union_template.html"


def render(u: dict) -> str:
    data = json.dumps(u, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE_PATH.read_text().replace(
        "window.__ARO_UNION__ || ", f"{data} || ", 1)


def cli(args) -> None:
    names = args.specs or permtree.ledgers()
    if not names:
        raise SystemExit("no permtree ledgers found (memory/permtree/*.jsonl) — "
                         "run a campaign first")
    missing = [n for n in names if not permtree._path(n).exists()]
    if missing:
        raise SystemExit(f"no ledger for: {', '.join(missing)} "
                         f"(have: {', '.join(permtree.ledgers()) or 'none'})")
    u = permtree.union(names)
    out = Path(args.out or "union-report.html")
    out.write_text(render(u))
    out.with_suffix(".json").write_text(
        json.dumps(u, ensure_ascii=False, indent=1))
    lanes = sorted(u["lanes"])
    print(f"union over {len(names)} ledger(s) → {out}")
    for wl in lanes:
        rows = u["lanes"][wl]
        acc = sum(1 for r in rows if r.get("verdict") == "accepted")
        print(f"  {wl}: {len(rows)} node(s) · {acc} accepted · "
              f"compounded Δ {u['realized'].get(wl, 0):+.2f}%")
    if u["open_cases"]:
        print("  open debt: " + ", ".join(
            f"{c['fn']}@{c['workload']}" for c in u["open_cases"]))
    else:
        print("  open debt: none")
