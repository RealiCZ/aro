"""Project lessons memory — a cross-run, cross-target knowledge base of what was
tried and what it cost, so future optimizations don't re-derive known dead ends or
regressions. Append-only JSONL at `memory/lessons.jsonl` (repo root), read back
into every generator prompt as "do not repeat these".

This is distinct from `store.py` (per-run records / pareto / agenda): lessons
persist across runs AND across targets, and are committed to git — the project's
accumulated optimization experience.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "memory" / "lessons.jsonl"


def _toks(s: str):
    return set(re.findall(r"[a-z0-9]{3,}", (s or "").lower()))


def _relevant(lesson_target: str, target) -> bool:
    """A lesson is in scope when no target is asked for, or it is GLOBAL (`*`), or
    it is the same target, or it shares a normalized token with the current target
    (repo family — e.g. two specs on the same repo share lessons via a common token,
    and a `salt/banderwagon` lesson surfaces for any `salt/...` spec). This is what
    makes the memory cross-target instead of exact-name-only."""
    if target is None or not lesson_target:
        return True
    if lesson_target == "*" or lesson_target == target:
        return True
    return bool(_toks(lesson_target) & _toks(target))


def append(target: str, change: str, verdict: str, delta_pct=None, note: str = "",
           gated=None, ir_delta_pct=None, profile_fingerprint=None) -> None:
    """Record one outcome as a durable lesson. Best-effort; never raises.

    `gated` marks a genuine architecture/scope objection (the critic's structured
    rubrics decide it at the call site). When given, it is written explicitly so
    the read side (`frontier._lesson_index`) never falls back to keyword-sniffing
    this row; None keeps the legacy row shape (historic freeform rows only).

    `ir_delta_pct` / `profile_fingerprint` are additive Ir-gate fields — only
    written when provided, so non-icount paths stay byte-identical to before.
    """
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "target": target,
            "change": (change or "").strip()[:240],
            "verdict": verdict,
            "delta_pct": (round(delta_pct, 3) if isinstance(delta_pct, (int, float)) else None),
            "note": (note or "").strip()[:240],
        }
        if gated is not None:
            rec["gated"] = bool(gated)
        if ir_delta_pct is not None and isinstance(ir_delta_pct, (int, float)):
            rec["ir_delta_pct"] = round(float(ir_delta_pct), 4)
        if profile_fingerprint:
            rec["profile_fingerprint"] = str(profile_fingerprint)[:120]
        with _PATH.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent(target=None, limit: int = 25) -> list:
    if not _PATH.exists():
        return []
    out = []
    for ln in _PATH.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if _relevant(r.get("target", ""), target):
            out.append(r)
    # Keep the most recent `limit`, but never let a flood of target-specific rows
    # evict the GLOBAL (`*`) lessons — those are the measurement-hygiene rules that
    # apply everywhere (e.g. per-worktree target dirs). Always retain them.
    globals_ = [r for r in out if r.get("target") == "*"]
    tail = out[-limit:]
    for r in globals_:
        if r not in tail:
            tail = [r] + tail
    return tail


def summary(target=None, limit: int = 25) -> str:
    """Natural-language digest fed into generator prompts: past dead ends and
    regressions (cross-run), so a round isn't wasted re-deriving them."""
    rs = recent(target, limit)
    if not rs:
        return ""
    lines = ["Lessons from past runs (cross-run memory — do NOT repeat a known dead "
             "end or regression; build on what won):"]
    for r in rs:
        d = (f" Δ{r['delta_pct']:+.2f}%"
             if isinstance(r.get("delta_pct"), (int, float)) else "")
        why = f" — {r['note']}" if r.get("note") else ""
        lines.append(f"  - [{r.get('verdict')}{d}] {r.get('change', '')[:140]}{why}")
    return "\n".join(lines)
