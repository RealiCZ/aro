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

When the target declares `terminal_bench_targets`, mergeable further requires
`terminal == TERMINAL_CONFIRMED` (criterion row-level Ir gate; plan §4/§7). Specs
without terminal config keep the legacy mergeable rule byte-identical. Terminal
fields (`terminal`, `bench_ir_rows`, `profile_fingerprint`) are stamped by
`aro terminal --update-manifest` or by `build_manifest(..., terminal_result=...)`.

Works on any run: a new run stamps `attempt` on each event (used directly); an old run
has no stamp, so the attempt index is derived by counting `attempt_started` in seq order.

    python3 -m aro manifest <out-dir> [--out manifest.json] [--spec targets/x.json]
    python3 -m aro terminal <spec> --baseline DIR --candidate DIR --update-manifest <out-dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from . import runlog
from .types import pick_reported_delta


def _attempt_of(e, counter):
    """The a<N> index for an event: the stamped `attempt` (new runs), else the running
    count of attempt_started seen so far (old runs). `counter` is a 1-element list."""
    if isinstance(e.get("attempt"), int):
        return e["attempt"]
    if e.get("event") == "attempt_started":
        counter[0] += 1
    return counter[0] or None


def _best_delta(deltas):
    """(metric, delta_pct) of the headline delta (rule: types.pick_reported_delta)."""
    d = pick_reported_delta(deltas)
    return (d.get("metric"), d.get("delta_pct")) if d else (None, None)


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


def is_mergeable(regime, critic_verdict, *, terminal=None,
                 terminal_required: bool = False) -> bool:
    """mergeable = byte-identical + critic pass [+ TERMINAL_CONFIRMED when configured].

    Specs without terminal config (`terminal_required=False`) keep the legacy rule.
    When terminal is required, absence of a CONFIRMED stamp keeps mergeable false
    so a PR cannot open before the criterion Ir gate runs.
    """
    base = (regime == "byte-identical") and (critic_verdict in (None, "pass"))
    if not terminal_required:
        return base
    from .terminal import TERMINAL_CONFIRMED
    return base and (terminal == TERMINAL_CONFIRMED)


def apply_terminal(manifest: dict, result, *,
                   terminal_required: bool = True) -> dict:
    """Stamp terminal fields onto every accepted entry and recompute mergeable.

    `result` is a TerminalResult or a dict from `TerminalResult.to_dict()` /
    a previously written terminal.json. Whole-checkout measurement — same stamp
    on every accepted edit (they share the candidate worktree under PR bundling).
    """
    if hasattr(result, "to_dict"):
        d = result.to_dict()
    else:
        d = dict(result)
    verdict = d.get("verdict")
    rows = dict(d.get("bench_ir_rows") or {})
    fp = d.get("profile_fingerprint")
    for a in manifest.get("accepted") or []:
        a["terminal"] = verdict
        a["bench_ir_rows"] = rows
        a["profile_fingerprint"] = fp
        a["mergeable"] = is_mergeable(
            a.get("regime"), a.get("critic_verdict"),
            terminal=verdict, terminal_required=terminal_required)
    # Top-level summary for the PR protocol (optional, additive).
    manifest["terminal"] = {
        "verdict": verdict,
        "bench_ir_rows": rows,
        "profile_fingerprint": fp,
    }
    return manifest


def build_manifest(out_dir, *, terminal_result=None,
                   terminal_required: bool = False) -> dict:
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

    # Optional terminal stamp (TerminalResult or dict).
    term_verdict = term_rows = term_fp = None
    if terminal_result is not None:
        if hasattr(terminal_result, "to_dict"):
            td = terminal_result.to_dict()
        else:
            td = dict(terminal_result)
        term_verdict = td.get("verdict")
        term_rows = dict(td.get("bench_ir_rows") or {})
        term_fp = td.get("profile_fingerprint")

    accepted, files_touched = [], []
    for order, (a, cid) in enumerate(advanced, 1):
        st = started.get(a, {})
        regime = st.get("regime")
        files, patch_path = _patch_files(out_dir, a, cid)
        metric, delta = _best_delta(verdicts.get((a, cid), []))
        critic_verdict = critics.get((a, cid))   # None if critic was off
        mergeable = is_mergeable(
            regime, critic_verdict,
            terminal=term_verdict, terminal_required=terminal_required)
        for f in files:
            if f not in files_touched:
                files_touched.append(f)
        entry = {
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
        }
        # Additive terminal fields only when the gate is in play (required or stamped).
        # Specs without terminal config keep the legacy entry shape byte-identical.
        if terminal_required or terminal_result is not None:
            entry["terminal"] = term_verdict
            entry["bench_ir_rows"] = dict(term_rows or {})
            entry["profile_fingerprint"] = term_fp
        accepted.append(entry)

    notes = (
        "Apply the accepted patches on baseline_ref, in `order` (they compound). "
        "accepted = correctness+speed PROVEN by the judge, NOT should-merge: only "
        "`mergeable:true` entries (byte-identical regime + critic pass"
        + (" + TERMINAL_CONFIRMED" if terminal_required else "")
        + ") are safe to "
        "PR directly; relaxed/pass-risk entries need a human call. Patch text is at "
        "patch_path (SEARCH/REPLACE blocks; `base-*` ids are seeded baseline, not "
        "candidates). Verify against events.jsonl — it is the source of truth.")

    out = {
        "spec": run_started.get("target") or out_dir.name,
        "baseline_ref": run_started.get("baseline_ref"),
        "run_id": run_started.get("run_id"),
        "generated_from": "events.jsonl (latest run_id slice)",
        "accepted": accepted,
        "files_touched": files_touched,
        "notes": notes,
    }
    if terminal_required or terminal_result is not None:
        out["terminal"] = {
            "verdict": term_verdict,
            "bench_ir_rows": dict(term_rows or {}),
            "profile_fingerprint": term_fp,
        }
    return out


def _resolve_terminal_required(args) -> bool:
    """When --spec is given and declares terminal_bench_targets, gate mergeable."""
    spath = getattr(args, "spec", None)
    if not spath:
        return False
    try:
        from . import spec as specmod
        from . import terminal as termmod
        raw = json.loads(Path(spath).read_text())
        sp = specmod.from_dict(raw)
        return termmod.has_terminal_config(sp)
    except Exception:
        return False


def _load_terminal_file(path: Optional[str]):
    if not path:
        return None
    return json.loads(Path(path).read_text())


def cli(args) -> None:
    out_dir = Path(args.out_dir)
    terminal_required = _resolve_terminal_required(args)
    terminal_result = _load_terminal_file(getattr(args, "terminal", None))
    # Auto-pick out_dir/terminal.json when present and no explicit --terminal.
    if terminal_result is None:
        auto = out_dir / "terminal.json"
        if auto.exists():
            terminal_result = json.loads(auto.read_text())
    m = build_manifest(out_dir, terminal_result=terminal_result,
                       terminal_required=terminal_required)
    out = args.out or str(out_dir / "manifest.json")
    Path(out).write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
    n = len(m["accepted"])
    ok = sum(1 for a in m["accepted"] if a["mergeable"])
    print(f"manifest → {out}")
    gate = "byte-identical + critic pass" + (
        " + TERMINAL_CONFIRMED" if terminal_required else "")
    print(f"  {n} accepted edit(s) · {ok} mergeable ({gate}) · "
          f"{n - ok} need human review")
    for a in m["accepted"]:
        flag = "MERGEABLE " if a["mergeable"] else "needs-review"
        d = f"{a['delta_pct']:+.2f}%" if a["delta_pct"] is not None else "?"
        term = f" terminal={a['terminal']}" if "terminal" in a else ""
        print(f"  [{flag}] {a['attempt']} {a['fn']} {d} ({a['regime']}/"
              f"critic={a['critic_verdict']}{term}) → {a['files']}")


if __name__ == "__main__":
    from .cli import main as _cli_main
    _cli_main(["manifest"] + sys.argv[1:])
