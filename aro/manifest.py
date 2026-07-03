"""aro manifest — the final accepted edit-set of a run, pre-assembled with provenance.

A run's truth is `events.jsonl`, but "what did this run actually change, and which of
it is safe to merge" is spread across the log: the wins are the `baseline_advanced`
events (the patches FOLDED into the compounding baseline), their diffs live in
`a<N>/patches/<id>.txt`, their Δ in `candidate_verdict`, their review verdict in
`critic`. Worse, candidate ids collide across attempts (`agent-r0-0` exists in every
a<N>), so mapping a win to its patch means knowing the attempt. This module does that
join once and writes `manifest.json` — the hand-off artifact: an agent turning a run
into a PR reads this instead of re-deriving the timeline.

Each accepted entry carries: order, attempt dir, candidate id, target fn, file(s), Δ,
oracle regime, critic verdict, a `mergeable` flag, and the patch path. Apply them on
`baseline_ref`, in `order` (they compound). **accepted = correctness+speed PROVEN, NOT
should-merge** — `mergeable` marks the byte-identical, cleanly-reviewed wins; the rest
(relaxed regime / critic pass-risk) need a human call before a PR.

Works on any run: a new run stamps `attempt` on each event (used directly); an old run
has no stamp, so the attempt index is derived by counting `attempt_started` in seq order.

    python3 -m aro manifest <out-dir> [--out manifest.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import runlog


def _attempt_of(e, counter):
    """The a<N> index for an event: the stamped `attempt` (new runs), else the running
    count of attempt_started seen so far (old runs). `counter` is a 1-element list."""
    if isinstance(e.get("attempt"), int):
        return e["attempt"]
    if e.get("event") == "attempt_started":
        counter[0] += 1
    return counter[0] or None


def _best_delta(deltas):
    """Direction-aware pick from a candidate_verdict's per-metric deltas: the improved
    metric with the largest |Δ| (a minimize win is very negative, a maximize win very
    positive); else the first metric. Returns (metric, delta_pct) or (None, None)."""
    if not deltas:
        return None, None
    improved = [d for d in deltas if d.get("improved")]
    d = max(improved, key=lambda x: abs(x.get("delta_pct", 0.0))) if improved else deltas[0]
    return d.get("metric"), d.get("delta_pct")


def _patch_files(out_dir: Path, attempt, cid: str):
    """The file paths a win's patch touches, parsed from its patches/<id>.txt. attempt
    None → the run-root patches/ (an `aro run`, no a<N> dirs)."""
    from . import patchfile
    base = (out_dir / f"a{attempt}") if attempt else out_dir
    pf = base / "patches" / (patchfile.safe_id(cid) + ".txt")
    if not pf.exists():
        return [], None
    edits = patchfile.parse(pf.read_text())
    rel = str(pf.relative_to(out_dir)) if pf.is_relative_to(out_dir) else str(pf)
    return [e.path for e in edits], rel


def build_manifest(out_dir) -> dict:
    out_dir = Path(out_dir)
    evs = runlog.load_run(out_dir)

    # First pass: derive each event's attempt and index the per-(attempt,id) facts.
    counter = [0]
    started, verdicts, critics, props = {}, {}, {}, {}
    advanced, run_started = [], {}
    for e in evs:
        a = _attempt_of(e, counter)
        ev = e.get("event")
        if ev == "run_started" and not run_started:
            run_started = e
        elif ev == "attempt_started":
            started[a] = e
        elif ev == "candidate_verdict":
            verdicts[(a, e.get("id"))] = e.get("deltas", [])
        elif ev == "critic":
            critics[(a, e.get("id"))] = e.get("verdict")
        elif ev == "candidate_proposed":
            props[(a, e.get("id"))] = e
        elif ev == "baseline_advanced":
            advanced.append((a, e.get("by")))

    accepted, files_touched = [], []
    for order, (a, cid) in enumerate(advanced, 1):
        st = started.get(a, {})
        regime = st.get("regime")
        files, patch_path = _patch_files(out_dir, a, cid)
        metric, delta = _best_delta(verdicts.get((a, cid), []))
        critic_verdict = critics.get((a, cid))   # None if critic was off
        mergeable = (regime == "byte-identical") and (critic_verdict in (None, "pass"))
        for f in files:
            if f not in files_touched:
                files_touched.append(f)
        accepted.append({
            "order": order,
            "attempt": (f"a{a}" if a else None),
            "id": cid,
            "fn": st.get("fn"),
            "files": files,
            "metric": metric,
            "delta_pct": (round(delta, 3) if isinstance(delta, (int, float)) else None),
            "regime": regime,
            "critic_verdict": critic_verdict,
            "mergeable": mergeable,
            "hypothesis": (props.get((a, cid), {}) or {}).get("hypothesis", ""),
            "patch_path": patch_path,
        })

    return {
        "spec": run_started.get("target") or out_dir.name,
        "baseline_ref": run_started.get("baseline_ref"),
        "run_id": run_started.get("run_id"),
        "generated_from": "events.jsonl (latest run_id slice)",
        "accepted": accepted,
        "files_touched": files_touched,
        "notes": (
            "Apply the accepted patches on baseline_ref, in `order` (they compound). "
            "accepted = correctness+speed PROVEN by the judge, NOT should-merge: only "
            "`mergeable:true` entries (byte-identical regime + critic pass) are safe to "
            "PR directly; relaxed/pass-risk entries need a human call. Patch text is at "
            "patch_path (SEARCH/REPLACE blocks; `base-*` ids are seeded baseline, not "
            "candidates). Verify against events.jsonl — it is the source of truth."),
    }


def main(argv) -> None:
    if not argv:
        raise SystemExit("usage: python3 -m aro manifest <out-dir> [--out manifest.json]")
    out_dir = Path(argv[0])
    m = build_manifest(out_dir)
    out = (argv[argv.index("--out") + 1] if "--out" in argv
           else str(out_dir / "manifest.json"))
    Path(out).write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
    n = len(m["accepted"])
    ok = sum(1 for a in m["accepted"] if a["mergeable"])
    print(f"manifest → {out}")
    print(f"  {n} accepted edit(s) · {ok} mergeable (byte-identical + critic pass) · "
          f"{n - ok} need human review")
    for a in m["accepted"]:
        flag = "MERGEABLE " if a["mergeable"] else "needs-review"
        d = f"{a['delta_pct']:+.2f}%" if a["delta_pct"] is not None else "?"
        print(f"  [{flag}] {a['attempt']} {a['fn']} {d} ({a['regime']}/"
              f"critic={a['critic_verdict']}) → {a['files']}")


if __name__ == "__main__":
    main(sys.argv[1:])
