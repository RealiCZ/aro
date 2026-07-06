"""permtree — L4c: the PERMANENT decision tree (the cross-run exhaustion ledger).

A single run's tree is derived from its events.jsonl and dies with the out-dir.
The permanent tree is the campaign-level, append-only ledger that survives runs:
every (workload, function, baseline-state) node's terminal verdict, evidence and
commentary, accumulated until the three exhaustion boundaries close
(docs/self-extending-search-design.md §3.3).

Storage: `memory/permtree/<spec>.jsonl` in the aro-py checkout (beside
memory/lessons.jsonl — the same institutional-memory convention). Append-only,
one JSON object per line; a node's CURRENT state is its LAST record (same
discipline as events.jsonl: writers append, readers reduce). Numbers are copied
verbatim from the run that produced them, with an `events` pointer back to the
out-dir so every figure remains auditable.

Node identity (design W5: node-level aggregation, evidence by reference):
    key = workload · fn · sha1(baseline edit-set)[:12]
A re-visit of the same fn on the SAME baseline state attaches to the same node;
after the baseline advances the fn is a NEW node (the object changed).
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

import os

REPO_ROOT = Path(__file__).resolve().parent.parent
_DIR = Path(os.environ.get("ARO_PERMTREE_DIR", REPO_ROOT / "memory" / "permtree"))


def baseline_state(edits) -> str:
    """Stable fingerprint of the cumulative accepted edit-set (the baseline the
    node was judged on). Empty set → 'origin'."""
    if not edits:
        return "origin"
    h = hashlib.sha1()
    for e in edits:
        h.update(e.path.encode()); h.update(b"\x00")
        h.update(e.search.encode()); h.update(b"\x00")
        h.update(e.replace.encode()); h.update(b"\x01")
    return h.hexdigest()[:12]


def node_key(workload: str, fn: str, base_state: str) -> str:
    return f"{workload}·{fn}·{base_state}"


def _path(spec_name: str) -> Path:
    return _DIR / f"{spec_name}.jsonl"


def record(spec_name: str, *, workload: str, fn: str, base_state: str,
           verdict: str, regime: str, delta=None, parent_delta=None,
           pct=None, files=(), probe_sha=None, hypothesis: str = "",
           critic=None, reflect=(), events_ref: str = "", run_id: str = "") -> dict:
    """Append one node observation. Returns the record written."""
    rec = {
        "key": node_key(workload, fn, base_state),
        "workload": workload, "fn": fn, "base": base_state,
        "verdict": verdict, "regime": regime,
        "delta": delta, "parent_delta": parent_delta, "pct": pct,
        "files": list(files), "probe_sha": probe_sha,
        "hypothesis": (hypothesis or "")[:400],
        "critic": critic,                      # [{rubric, finding, severity}] | None
        "reflect": [str(r)[:200] for r in reflect],
        "events": events_ref,                  # out-dir (+ attempt idx) — the audit trail
        "run_id": run_id,
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _DIR.mkdir(parents=True, exist_ok=True)
    with _path(spec_name).open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load(spec_name: str) -> list:
    """All observations, file order. [] when no ledger exists yet."""
    p = _path(spec_name)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def nodes(spec_name: str) -> dict:
    """The tree's CURRENT state: node key → last observation, plus a `visits`
    count so a re-judged node shows its history depth."""
    cur: dict = {}
    for rec in load(spec_name):
        k = rec.get("key")
        if not k:
            continue
        rec = dict(rec)
        rec["visits"] = cur.get(k, {}).get("visits", 0) + 1
        cur[k] = rec
    return cur


def ledgers() -> list:
    """Spec names that have a ledger on disk (sorted)."""
    if not _DIR.exists():
        return []
    return sorted(p.stem for p in _DIR.glob("*.jsonl"))


def union(spec_names=None) -> dict:
    """The CROSS-CAMPAIGN view: merge any number of ledgers into one structure.

    Node keys are already namespaced by workload (spec name, or spec+vN for
    synthetic variants), so merging is collision-free by construction. Returns:
      specs      — the ledgers merged
      lanes      — workload → [current node rows, heaviest pct first]
      fn_matrix  — fn → {workload: current row} (the side-by-side judgment view)
      open_cases — latest-per-(workload, fn) rows still noise-limited (global debt)
      accepted   — every accepted current node across all lanes
      realized   — workload → compounded accepted Δ share (1 - Π(1+δ/100), in %):
                   an approximation from ledger deltas (the exact number lives in
                   each run's events.jsonl; this one is for cross-lane comparison)
    """
    names = list(spec_names) if spec_names else ledgers()
    lanes: dict = {}
    latest: dict = {}
    for spec_name in names:
        for k, rec in nodes(spec_name).items():
            lanes.setdefault(rec.get("workload") or spec_name, []).append(rec)
        for rec in load(spec_name):
            latest[(rec.get("workload"), rec.get("fn"))] = rec
    for wl in lanes:
        lanes[wl].sort(key=lambda r: -(r.get("pct") or 0.0))
    fn_matrix: dict = {}
    for wl, rows in lanes.items():
        for r in rows:
            fn_matrix.setdefault(r.get("fn") or "?", {})[wl] = r
    open_cases = [r for r in latest.values() if r.get("verdict") in _OPEN_VERDICTS]
    accepted = [r for rows in lanes.values() for r in rows
                if r.get("verdict") == "accepted"]
    realized = {}
    for wl, rows in lanes.items():
        prod = 1.0
        for r in rows:
            d = r.get("delta")
            if r.get("verdict") == "accepted" and isinstance(d, (int, float)):
                prod *= (1.0 + d / 100.0)
        realized[wl] = round((1.0 - prod) * 100.0, 2)
    return {"specs": names, "lanes": lanes, "fn_matrix": fn_matrix,
            "open_cases": open_cases, "accepted": accepted, "realized": realized}


# --- the exhaustion proof (three boundaries, design §3.3) --------------------------

_OPEN_VERDICTS = {"noise-limited"}          # a pending case: real signal, unresolved
_CLOSED_VERDICTS = {"accepted", "within-noise", "regressed", "verify-failed",
                    "build-failed", "rejected", "parent-regressed", "unlocated",
                    "no-candidate"}


def closure(spec_name: str, *, floor_pct=None, headroom_pct=None,
            workload_factory_state: str = "single-workload") -> dict:
    """The campaign's exhaustion-proof state, computed from the permanent tree +
    the latest profile quantities. Three boundaries:
      1. untouchable floor  — proven not-ours share (from the profile buckets)
      2. measurement floor  — every noise-limited node either probe-rescued
                              (micro-proven/…) or recorded probe-capped
      3. coverage closure   — the workload factory is dry (L4b; 'single-workload'
                              until it lands)
    All three closed + zero open headroom = exhaustion PROVEN."""
    ns = nodes(spec_name)
    # Open cases are judged on the LATEST observation per (workload, fn): a
    # noise-limited node keyed to a SUPERSEDED baseline must not block closure
    # forever once a newer observation of the same fn exists (review finding).
    latest: dict = {}
    for rec in load(spec_name):
        latest[(rec.get("workload"), rec.get("fn"))] = rec
    open_cases = [n for n in latest.values() if n.get("verdict") in _OPEN_VERDICTS]
    rescued = [n for n in ns.values() if n.get("regime") == "micro-proven"]
    b1 = {"name": "untouchable-floor", "closed": floor_pct is not None,
          "floor_pct": floor_pct}
    b2 = {"name": "measurement-floor", "closed": not open_cases,
          "open_cases": [n["fn"] for n in open_cases],
          "rescued": [n["fn"] for n in rescued]}
    b3 = {"name": "coverage-closure",
          "closed": workload_factory_state == "dry",
          "state": workload_factory_state}
    drained = (headroom_pct is not None and headroom_pct <= 2.0)
    return {
        "boundaries": [b1, b2, b3],
        "headroom_pct": headroom_pct,
        "exhausted": bool(b1["closed"] and b2["closed"] and b3["closed"] and drained),
    }
