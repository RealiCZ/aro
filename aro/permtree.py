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
           critic=None, reflect=(), events_ref: str = "", run_id: str = "",
           ir_delta_pct=None, profile_fingerprint=None,
           env_fingerprint=None, backend=None) -> dict:
    """Append one node observation. Returns the record written.

    `ir_delta_pct` / `profile_fingerprint` / `env_fingerprint` / `backend` are
    additive fields — only written when provided so legacy paths stay
    byte-identical to before. `env_fingerprint` is the host tool triple;
    `backend` identifies candidate generation, not the model-agnostic judge.
    """
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
    if ir_delta_pct is not None and isinstance(ir_delta_pct, (int, float)):
        rec["ir_delta_pct"] = round(float(ir_delta_pct), 4)
    if profile_fingerprint:
        rec["profile_fingerprint"] = str(profile_fingerprint)[:120]
    if env_fingerprint:
        rec["env_fingerprint"] = str(env_fingerprint)[:200]
    if backend:
        rec["backend"] = str(backend)[:120]
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


def open_debts(rows) -> list:
    """Latest-per-(workload, fn) observations still OPEN across all lanes:
    noise-limited measurement debt, never-tried residue, zero-candidate
    non-judgments (nothing ever reached the judge — generator dry/down), and
    no-coverage (probe does not exercise the patched files — needs a better
    probe, not a closed account). The pending-first walk pays these; `aro next`
    decides whether paying is still possible."""
    latest: dict = {}
    for r in rows:
        if r.get("fn"):
            latest[(r.get("workload"), r["fn"])] = r
    return [r for r in latest.values()
            if r.get("verdict") in ("noise-limited", "no-attempt",
                                    "no-candidate", "no-coverage")]


def debt_keys(rows) -> list:
    """Stable identity of the open debt set (sorted workload·fn) — recorded in
    the campaign state so `aro next` can tell "new debts" from "the same debts
    the last campaign already failed to move" (the probe-capped floor)."""
    return sorted(f"{d.get('workload')}·{d.get('fn')}" for d in open_debts(rows))


# --- campaign closing state (the next-action oracle's input) -----------------------

def state_path(spec_name: str) -> Path:
    return _DIR / f"{spec_name}.state.json"


def record_state(spec_name: str, **fields) -> dict:
    """Overwrite the spec's campaign-state file (last campaign wins — the file
    answers "where did the LAST run leave things", not history; history is the
    ledger). `aro next` reads this to know the factory closure state, the run's
    out-dir (manifest location) and whether the harvest was marked done."""
    st = {**fields, "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    _DIR.mkdir(parents=True, exist_ok=True)
    state_path(spec_name).write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n")
    return st


def load_state(spec_name: str):
    """The last campaign's closing state, or None."""
    p = state_path(spec_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def mark_state(spec_name: str, **fields):
    """Merge fields into the existing state file (e.g. harvested=<ts>)."""
    st = load_state(spec_name) or {}
    st.update(fields)
    _DIR.mkdir(parents=True, exist_ok=True)
    state_path(spec_name).write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n")
    return st


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
      conflicts  — fns accepted in one lane but regressed/rejected in another:
                   the MERGE GATE input — a cross-lane contradiction must be
                   disclosed (or block the recommendation) before any PR
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
                if r.get("verdict") in _ACCEPT_VERDICTS]
    realized = {}
    for wl, rows in lanes.items():
        prod = 1.0
        for r in rows:
            d = r.get("delta")
            if r.get("verdict") in _ACCEPT_VERDICTS and isinstance(d, (int, float)):
                prod *= (1.0 + d / 100.0)
        realized[wl] = round((1.0 - prod) * 100.0, 2)
    conflicts = []
    for fn, cells in sorted(fn_matrix.items()):
        vs = {wl: (r.get("verdict") or "") for wl, r in cells.items()}
        if (len(vs) >= 2 and any(v in _ACCEPT_VERDICTS for v in vs.values())
                and any(v in _CONFLICT_VERDICTS for v in vs.values())):
            conflicts.append({"fn": fn, "verdicts": vs})
    return {"specs": names, "lanes": lanes, "fn_matrix": fn_matrix,
            "open_cases": open_cases, "accepted": accepted, "realized": realized,
            "conflicts": conflicts}


# --- the exhaustion proof (three boundaries, design §3.3) --------------------------

# Pending cases: noise-limited is real signal unresolved; no-candidate is a
# NON-judgment — zero candidates ever reached the judge (generator dry or
# hard-down), so nothing about the function was actually decided; no-coverage
# means the probe never exercised the patched files — open until a better
# probe is written, not settled as closed. None of these may close an
# exhaustion boundary (rex5-01: a quota-dead run wrote 8 no-candidate rows
# that read as "closed" until this).
_OPEN_VERDICTS = {"noise-limited", "no-candidate", "no-coverage"}
# A lane saying "win" while another says one of these is a CONTRADICTION the
# merge decision must see (build/verify failures are non-judgments, not these).
_CONFLICT_VERDICTS = {"regressed", "regressed-ir", "rejected", "parent-regressed"}
_ACCEPT_VERDICTS = {"accepted", "accepted-ir"}
# refuted-by-icount: historical wall-clock claim closed by Ir gate / CodSpeed —
# CLOSED (not open debt) and NOT an accept (does not fold / bank as a win).
_CLOSED_VERDICTS = {"accepted", "within-noise", "regressed", "verify-failed",
                    "build-failed", "rejected", "parent-regressed", "unlocated",
                    "accepted-ir", "neutral-ir", "regressed-ir",
                    "refuted-by-icount",
                    # pre-PR criterion Ir gate (plan §4) — terminal, not accept
                    "TERMINAL_CONFIRMED", "TERMINAL_UNTOUCHED",
                    "TERMINAL_REGRESSED", "TERMINAL_MIXED",
                    "TERMINAL_TEST_FAILED", "TERMINAL_CONTROL_ANOMALY"}


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
