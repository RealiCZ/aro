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
    open_cases = [n for n in ns.values() if n["verdict"] in _OPEN_VERDICTS]
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
