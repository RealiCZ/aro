"""Memory: the persistent record store + knowledge base.

File-backed, stdlib-only, append-only, resumable. Everything lives under one dir:
  - records.jsonl     — one JSON object per recorded outcome
  - pareto.txt        — accepted (Pareto-front) candidate ids, one per line
  - floors.json       — the A/A-calibrated noise floors
  - patches/<id>.txt  — the patch for each candidate (NoOp or edits)

On open we reconstruct in-memory state from these files so a re-run resumes where
it left off. Parsing is defensive: a malformed line is skipped, not fatal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .types import Candidate, Direction, Edit, EvalOutcome, NoiseFloors, Verdict


class Memory:
    def __init__(self, dir):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.dir / "records.jsonl"
        self.pareto_path = self.dir / "pareto.txt"
        self.floors_path = self.dir / "floors.json"
        self.patches_dir = self.dir / "patches"
        self.rows: list[dict] = []
        self.pareto: list[str] = []
        self.floors = NoiseFloors()
        self.agenda_path = self.dir / "agenda.jsonl"
        self.directions: list[Direction] = []
        self._load()

    def _load(self) -> None:
        if self.records_path.exists():
            for line in self.records_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self.rows.append(json.loads(line))
                except Exception:
                    continue
        if self.pareto_path.exists():
            for line in self.pareto_path.read_text().splitlines():
                pid = line.strip()
                if pid and pid not in self.pareto:
                    self.pareto.append(pid)
        if self.floors_path.exists():
            try:
                self.floors.floors = json.loads(self.floors_path.read_text())
            except Exception:
                pass
        if self.agenda_path.exists():
            for line in self.agenda_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    self.directions.append(Direction(
                        id=d["id"], direction=d["direction"],
                        rationale=d.get("rationale", ""), source=d.get("source", ""),
                        status=d.get("status", "open"), round=d.get("round", 0)))
                except Exception:
                    continue

    def set_floors(self, floors: NoiseFloors) -> None:
        self.floors = floors
        self.floors_path.write_text(json.dumps(floors.floors, indent=2))

    def record(self, cand: Candidate, outcome: EvalOutcome) -> None:
        row = {
            "id": cand.id,
            "verdict": outcome.verdict.value,
            "hypothesis": cand.hypothesis,
            "metrics": [
                {"metric": d.metric, "delta_pct": d.delta_pct,
                 "ci_low_pct": d.ci_low_pct, "ci_high_pct": d.ci_high_pct,
                 "floor_pct": d.floor_pct,
                 "improved": d.improved, "regressed": d.regressed}
                for d in outcome.deltas
            ],
            "notes": outcome.notes,
        }
        with self.records_path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._dump_patch(cand)

        if outcome.verdict == Verdict.ACCEPTED and cand.id not in self.pareto:
            self.pareto.append(cand.id)
            self.pareto_path.write_text("\n".join(self.pareto) + "\n")

        self.rows.append(row)

    def pareto_ids(self) -> list[str]:
        return sorted(set(self.pareto))

    def accepted_edits(self) -> list:
        """Rebuild the cumulative accepted patch, in acceptance order, from pareto
        + patches/ — so a resumed run starts from the advanced baseline instead of
        scratch (compounding survives across runs, not just within one)."""
        edits = []
        for pid in self.pareto:
            pf = self.patches_dir / (_safe(pid) + ".txt")
            if pf.exists():
                edits.extend(_parse_patch_file(pf.read_text()))
        return edits

    def summary(self) -> str:
        """A short natural-language summary fed to the generator next round."""
        if not self.rows:
            base = "memory empty (first round): no candidates tried yet."
            ag = self._agenda_lines()
            return base + ("\n" + "\n".join(ag) + "\n" if ag else "")

        total = len(self.rows)
        accepted = sum(1 for r in self.rows if r["verdict"] == "accepted")
        within = sum(1 for r in self.rows if r["verdict"] == "within-noise")
        failed = total - accepted - within

        lines = [f"tried={total} accepted={accepted} "
                 f"within-noise={within} failed={failed}"]

        front = self.pareto_ids()
        if not front:
            lines.append("pareto: (empty)")
        else:
            lines.append("pareto front:")
            for pid in front:
                bd = self._best_delta(pid)
                if bd:
                    lines.append(f"  {pid} best {bd[0]} {bd[1]:+.2f}%")
                else:
                    lines.append(f"  {pid} (no metrics)")

        dead: list[str] = []
        for r in self.rows:
            if r["verdict"] in ("within-noise", "verify-failed"):
                first = (r.get("hypothesis") or "").strip().splitlines()
                h = first[0] if first else ""
                if h and h not in dead:
                    dead.append(h)
                    if len(dead) >= 5:
                        break
        if dead:
            lines.append("dead ends (do not repeat):")
            for h in dead:
                lines.append(f"  - {h[:100]}")

        lines += self._agenda_lines()
        return "\n".join(lines) + "\n"

    # --- agenda (forward-looking research directions) ------------------------

    def next_direction_id(self) -> str:
        n = 0
        for d in self.directions:
            if d.id.startswith("d") and d.id[1:].isdigit():
                n = max(n, int(d.id[1:]))
        return f"d{n + 1}"

    def add_directions(self, items: list) -> list:
        """items: dicts {direction, rationale, source, round}. Dedup by normalized
        text, assign ids, persist, and return the Directions actually added —
        keeping the agenda from ballooning across rounds."""
        seen = {d.direction.strip().lower() for d in self.directions}
        added = []
        for it in items:
            text = (it.get("direction") or "").strip()
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            d = Direction(id=self.next_direction_id(), direction=text,
                          rationale=(it.get("rationale") or "").strip(),
                          source=it.get("source", ""), round=it.get("round", 0))
            self.directions.append(d)   # appended first so the next id increments
            added.append(d)
        if added:
            self._dump_agenda()
        return added

    def resolve_direction(self, id: str, status: str) -> None:
        """Mark a direction done/dropped so it stops being offered next round."""
        for d in self.directions:
            if d.id == id:
                d.status = status
        self._dump_agenda()

    def open_directions(self, limit=None) -> list[Direction]:
        out = [d for d in self.directions if d.status == "open"]
        return out[:limit] if limit else out

    def _agenda_lines(self, limit: int = 5) -> list:
        out = []
        for d in self.open_directions(limit=limit):
            why = f"  (why: {d.rationale[:80]})" if d.rationale else ""
            out.append(f"  - [{d.id}] {d.direction[:100]}{why}")
        if out:
            out.insert(0, "open agenda (try these next, highest-leverage first):")
        return out

    def _dump_agenda(self) -> None:
        with self.agenda_path.open("w") as f:
            for d in self.directions:
                f.write(json.dumps(
                    {"id": d.id, "direction": d.direction, "rationale": d.rationale,
                     "source": d.source, "status": d.status, "round": d.round},
                    ensure_ascii=False) + "\n")

    # --- internals -----------------------------------------------------------

    def _dump_patch(self, cand: Candidate) -> None:
        self.patches_dir.mkdir(parents=True, exist_ok=True)
        out = []
        if cand.patch.is_noop:
            out.append("NoOp")
        else:
            for i, e in enumerate(cand.patch.edits, 1):
                out.append(f"--- edit {i} ---")
                out.append(f"path: {e.path}")
                out.append("<<<<<<< SEARCH")
                out.append(e.search)
                out.append("=======")
                out.append(e.replace)
                out.append(">>>>>>> REPLACE")
        (self.patches_dir / f"{_safe(cand.id)}.txt").write_text("\n".join(out) + "\n")

    def _best_delta(self, pid: str) -> Optional[tuple]:
        """The metric to summarize for this candidate — DIRECTION-AWARE. Picking the
        most-negative Δ is wrong for a maximize objective (it would report the worst
        direction as 'best'). The judge's `improved` flag is already direction-correct,
        so: among improved metrics pick the largest improvement (max |Δ|, since a
        minimize win is very negative and a maximize win is very positive); if none
        improved, report the primary objective (the first metric = the goal metric)."""
        for r in reversed(self.rows):
            if r["id"] == pid:
                ms = r["metrics"]
                if not ms:
                    return None
                improved = [m for m in ms if m.get("improved")]
                m = (max(improved, key=lambda x: abs(x["delta_pct"]))
                     if improved else ms[0])
                return (m["metric"], m["delta_pct"])
        return None


def _safe(cid: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in cid)


def _parse_patch_file(text: str) -> list:
    """Parse a patches/<id>.txt dump (NoOp or SEARCH/REPLACE blocks) into Edits."""
    lines = text.split("\n")
    edits, i = [], 0
    while i < len(lines):
        if lines[i].startswith("path: "):
            path = lines[i][len("path: "):]
            i += 1
            if i < len(lines) and lines[i] == "<<<<<<< SEARCH":
                i += 1
                search = []
                while i < len(lines) and lines[i] != "=======":
                    search.append(lines[i]); i += 1
                i += 1
                replace = []
                while i < len(lines) and lines[i] != ">>>>>>> REPLACE":
                    replace.append(lines[i]); i += 1
                edits.append(Edit(path, "\n".join(search), "\n".join(replace)))
        i += 1
    return edits
