"""Project lessons memory — a cross-run, cross-target knowledge base of what was
tried and what it cost, so future optimizations don't re-derive known dead ends or
regressions. Append-only JSONL at `memory/lessons.jsonl` (repo root), read back
into every generator prompt as "do not repeat these".

This is distinct from `store.py` (per-run records / pareto / agenda): lessons
persist across runs AND across targets, and are committed to git — the project's
accumulated optimization experience.

Polarity principle (T51): suppression (removing work from the frontier) requires
strong evidence; information (prompt context for the generator) is cheap. Scoping
here decides what is *recalled*; the frontier's tried-bucket gate decides what may
*suppress*. See `frontier.bucket_functions` and skill/references/run-data.md.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "memory" / "lessons.jsonl"


def _norm_repo(repo) -> str:
    """Stable string for same-repo comparison. Empty when unknown — never guess."""
    if not repo:
        return ""
    try:
        return str(Path(repo).expanduser().resolve())
    except Exception:
        return str(repo).strip().rstrip("/")


def _relevant(lesson_target: str, target, *, lesson_repo: str = "",
              target_repo: str = "") -> bool:
    """Whether a lesson is in scope for *recall* (prompt / index), not suppression.

    Same-target is EXACT name match only (no name-token fuzzy overlap — that
    pulled `salt`/`banderwagon` lessons into `salt-msm` and emptied the frontier).
    Also recalled: GLOBAL (`*`) lessons, and different targets that share a
    stamped repo path (informational only — the frontier never buckets those).
    Other repos are excluded entirely. Missing repo stamps never invent a match.
    """
    if target is None or not lesson_target:
        return True
    if lesson_target == "*" or lesson_target == target:
        return True
    lr = _norm_repo(lesson_repo)
    tr = _norm_repo(target_repo)
    if lr and tr and lr == tr:
        return True
    return False


def append(target: str, change: str, verdict: str, delta_pct=None, note: str = "",
           gated=None, ir_delta_pct=None, profile_fingerprint=None,
           env_fingerprint=None, backend=None, baseline_sha=None,
           repo=None) -> None:
    """Record one outcome as a durable lesson. Best-effort; never raises.

    `gated` marks a genuine architecture/scope objection (the critic's structured
    rubrics decide it at the call site). When given, it is written explicitly so
    the read side (`frontier._lesson_index`) never falls back to keyword-sniffing
    this row; None keeps the legacy row shape (historic freeform rows only).

    `ir_delta_pct` / `profile_fingerprint` / `env_fingerprint` / `backend` are
    additive fields — only written when provided, so legacy paths stay
    byte-identical to before. `env_fingerprint` is the host tool triple
    (codspeed/cargo-codspeed/valgrind/rustc); keep separate from
    `profile_fingerprint` (Cargo profile + rustc). `backend` identifies the
    generation CLI (and model when known), not the model-agnostic judge.

    `baseline_sha` / `repo` (T51): stamp the campaign baseline and target repo
    so the frontier can require strong evidence before suppressing a function.
    Absent on legacy rows → informational only (never tried-bucket).
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
        if env_fingerprint:
            rec["env_fingerprint"] = str(env_fingerprint)[:200]
        if backend:
            rec["backend"] = str(backend)[:120]
        if baseline_sha:
            rec["baseline_sha"] = str(baseline_sha)[:64]
        nr = _norm_repo(repo)
        if nr:
            rec["repo"] = nr
        with _PATH.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent(target=None, limit: int = 25, repo=None) -> list:
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
        if _relevant(r.get("target", ""), target,
                     lesson_repo=r.get("repo", "") or "",
                     target_repo=repo or ""):
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


def summary(target=None, limit: int = 25, repo=None) -> str:
    """Natural-language digest fed into generator prompts: past dead ends and
    regressions (cross-run), so a round isn't wasted re-deriving them.

    Includes exact-target, same-repo (informational), and global lessons.
    Does NOT decide frontier suppression — that is the tried-bucket gate.
    """
    rs = recent(target, limit, repo=repo)
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
