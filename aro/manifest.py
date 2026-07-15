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

When the target declares `terminal_bench_targets`, mergeable further requires a
tool-written `terminal_stamp` whose `verdict == TERMINAL_CONFIRMED` (criterion
row-level Ir gate; plan §4/§7). A bare/legacy `"terminal"` string without a stamp
is ignored for mergeability (hand-edited fields are inert). Specs without terminal
config keep the legacy mergeable rule byte-identical. Terminal fields
(`terminal`, `bench_ir_rows`, `profile_fingerprint`, `terminal_stamp`) are stamped
by `aro terminal --update-manifest` or by `build_manifest(..., terminal_result=...,
terminal_source=...)`.

Outlier quarantine: an accepted entry whose |Δ| exceeds `outlier_quarantine_pct`
(default **5.0 even when the field is absent** — a quarantine nobody declares
protects nobody; explicit `0` disables) is forced to `mergeable=false` with an
additive `quarantine` reason, regardless of regime/critic/terminal. Decided inside
`resolve_mergeability` (same choke point as regime/critic/terminal) and applied
in both `build_manifest` and `apply_terminal` so the two paths cannot diverge.

Works on any run: a new run stamps `attempt` on each event (used directly); an old run
has no stamp, so the attempt index is derived by counting `attempt_started` in seq order.

    python3 -m aro manifest <out-dir> [--out manifest.json] [--spec targets/x.json]
    python3 -m aro terminal <spec> --baseline DIR --candidate DIR --update-manifest <out-dir>
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

from . import runlog
from .types import pick_reported_delta

# Default-ON tripwire: |Δ| above this % is auto-quarantined. Deliberately not
# "absent = legacy off" — see module docstring and docs/OPERATIONS.md.
DEFAULT_OUTLIER_QUARANTINE_PCT = 5.0


@dataclasses.dataclass(frozen=True)
class MergeDecision:
    """Single choke-point result: mergeable boolean + ordered human-readable reasons.

    mergeable=True iff reasons is empty. Reasons are independent (all applicable
    blocks are listed), e.g. regime, critic, terminal stamp, outlier.
    """
    mergeable: bool
    reasons: list  # list[str]; empty when mergeable


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


def outlier_quarantine_reason(delta_pct, threshold_pct) -> Optional[str]:
    """Reason string if |Δ| exceeds threshold; None if tripwire off or under.

    threshold_pct <= 0 (explicit 0 in the spec) disables the tripwire.
    """
    if threshold_pct is None or float(threshold_pct) <= 0:
        return None
    if not isinstance(delta_pct, (int, float)):
        return None
    thr = float(threshold_pct)
    if abs(delta_pct) > thr:
        return f"outlier: |Δ|={abs(delta_pct):.3f}% > {thr}%"
    return None


def apply_outlier_quarantine(entry: dict, *, threshold_pct: float) -> dict:
    """Force mergeable=false + set quarantine when |Δ| is an outlier.

    Never promotes mergeable (only forces false). Non-outlier entries lose any
    prior `quarantine` key so re-serialization stays free of the additive field.
    Prefer `resolve_mergeability` for new call sites (single choke point).
    """
    reason = outlier_quarantine_reason(entry.get("delta_pct"), threshold_pct)
    if reason:
        entry["mergeable"] = False
        entry["quarantine"] = reason
    else:
        entry.pop("quarantine", None)
    return entry


def _apply_merge_decision(entry: dict, dec: MergeDecision) -> dict:
    """Stamp mergeable + quarantine from a MergeDecision (quarantine shape unchanged)."""
    entry["mergeable"] = dec.mergeable
    oq = next((r for r in dec.reasons if r.startswith("outlier:")), None)
    if oq:
        entry["quarantine"] = oq
    else:
        entry.pop("quarantine", None)
    return entry


def status_flag(entry: dict) -> str:
    """CLI status label for one accepted entry (aligned MERGEABLE / needs-review).

    Single-reason labels are byte-identical to the pre-consolidation strings.
    When multiple review reasons apply, each is appended in its own parentheses
    so the CLI can surface all of them (e.g. outlier + unstamped terminal).
    """
    if entry.get("mergeable"):
        return "MERGEABLE "
    parts = []
    if entry.get("quarantine"):
        parts.append("outlier")
    # Loud: bare/legacy terminal string without a tool-written stamp is inert
    # for mergeability and must surface as unstamped, not a silent needs-review.
    if entry.get("terminal") is not None and not entry.get("terminal_stamp"):
        parts.append("unstamped terminal")
    if not parts:
        return "needs-review"
    return "needs-review " + " ".join(f"({p})" for p in parts)


def terminal_file_sha256(path) -> str:
    """Hex digest of the terminal.json file bytes (stamp integrity)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_terminal_stamp(verdict, source, sha256: str) -> dict:
    """Tool-written stamp: verdict + source path + content hash of that file."""
    return {
        "verdict": verdict,
        "source": str(source),
        "sha256": str(sha256),
    }


def build_terminal_stamp_from_source(source, *,
                                     control_lanes=None,
                                     control_bound_pct=None) -> dict:
    """Read terminal.json, verify integrity, return stamp (verdict/source/sha256).

    When `control_lanes` is provided (including `[]`), verification is
    lane-aware — required for mergeable-unlocking ingestion. Lane-less verify
    alone is not sufficient for mergeability (control-laundering channel).

    Raises TerminalError on content tamper; OSError on missing/unreadable file.
    """
    from .terminal import verify_terminal_doc
    sp = Path(source)
    raw = sp.read_bytes()
    doc = json.loads(raw)
    verify_terminal_doc(
        doc, control_lanes=control_lanes, control_bound_pct=control_bound_pct)
    return make_terminal_stamp(
        doc.get("verdict"), sp, hashlib.sha256(raw).hexdigest())


def resolve_mergeability(entry, *, regime, critic_verdict, terminal_required,
                         terminal_stamp=None, terminal=None,
                         outlier_threshold_pct=None) -> MergeDecision:
    """Single choke point: regime + critic + terminal stamp + outlier → decision.

    Returns mergeable=True with empty reasons only when every gate passes.
    Reasons are ordered and independent (all applicable failures are listed):

      - ``"regime not byte-identical"``
      - ``"critic rejected"``
      - ``"unstamped terminal (hand-edited field ignored)"``
      - ``"terminal not stamped-CONFIRMED"``
      - ``"outlier: |Δ|=X% > Y%"`` (same string as ``outlier_quarantine_reason``)

    Specs without terminal config (`terminal_required=False`) skip terminal gates.
    When terminal is required, only a tool-written `terminal_stamp` whose
    `verdict == TERMINAL_CONFIRMED` unlocks mergeable. The bare/legacy
    `terminal=` string is ignored for mergeability — hand-edited fields must
    not open a PR. Outlier uses the same threshold semantics as
    ``outlier_quarantine_reason`` (`None`/`<=0` disables; strict `>`).

    `entry` supplies `delta_pct` for the outlier check (other fields optional).
    """
    reasons = []
    if regime != "byte-identical":
        reasons.append("regime not byte-identical")
    if critic_verdict not in (None, "pass"):
        reasons.append("critic rejected")
    if terminal_required:
        from .terminal import TERMINAL_CONFIRMED
        if not isinstance(terminal_stamp, dict):
            reasons.append("unstamped terminal (hand-edited field ignored)")
        elif terminal_stamp.get("verdict") != TERMINAL_CONFIRMED:
            reasons.append("terminal not stamped-CONFIRMED")
    # `terminal` is intentionally unused for the decision (hand-edited inert);
    # kept in the signature so callers can pass the display field unchanged.
    del terminal
    oq = outlier_quarantine_reason(
        (entry or {}).get("delta_pct"), outlier_threshold_pct)
    if oq:
        reasons.append(oq)
    return MergeDecision(mergeable=(not reasons), reasons=reasons)


def is_mergeable(regime, critic_verdict, *, terminal=None,
                 terminal_required: bool = False,
                 terminal_stamp=None) -> bool:
    """mergeable = byte-identical + critic pass [+ stamped TERMINAL_CONFIRMED].

    Thin wrapper over ``resolve_mergeability`` without the outlier gate (no
    entry / threshold). Callers that need quarantine must pass
    ``outlier_threshold_pct`` via ``resolve_mergeability`` or apply
    ``apply_outlier_quarantine`` after.
    """
    return resolve_mergeability(
        {},
        regime=regime,
        critic_verdict=critic_verdict,
        terminal_required=terminal_required,
        terminal_stamp=terminal_stamp,
        terminal=terminal,
        outlier_threshold_pct=None,
    ).mergeable


def apply_terminal(manifest: dict, result, *,
                   terminal_required: bool = True,
                   outlier_quarantine_pct: float = DEFAULT_OUTLIER_QUARANTINE_PCT,
                   source=None,
                   control_lanes=None,
                   control_bound_pct=None,
                   ) -> dict:
    """Stamp terminal fields onto every accepted entry and recompute mergeable.

    `result` is a TerminalResult or a dict from `TerminalResult.to_dict()` /
    a previously written terminal.json. Whole-checkout measurement — same stamp
    on every accepted edit (they share the candidate worktree under PR bundling).

    When `source` is the path to the terminal.json file on disk, each entry gets
    an additive `terminal_stamp` `{verdict, source, sha256}` (sha256 of the file
    bytes). Without `source`, the legacy flat `terminal` field is still written
    for display, but mergeability stays false under `terminal_required` (no stamp).
    Mergeable-unlocking callers must pass `control_lanes` (possibly `[]`) so the
    stamp path is lane-aware.

    Outlier quarantine uses the same threshold via `resolve_mergeability` as
    `build_manifest` so the two paths cannot diverge on quarantine decisions.
    """
    if hasattr(result, "to_dict"):
        d = result.to_dict()
    else:
        d = dict(result)
    verdict = d.get("verdict")
    rows = dict(d.get("bench_ir_rows") or {})
    fp = d.get("profile_fingerprint")

    stamp = None
    if source is not None:
        stamp = build_terminal_stamp_from_source(
            source,
            control_lanes=control_lanes,
            control_bound_pct=control_bound_pct,
        )
        # Prefer the verified file's verdict for both stamp and display fields.
        verdict = stamp.get("verdict", verdict)

    for a in manifest.get("accepted") or []:
        a["terminal"] = verdict
        a["bench_ir_rows"] = rows
        a["profile_fingerprint"] = fp
        if stamp is not None:
            a["terminal_stamp"] = dict(stamp)
        else:
            a.pop("terminal_stamp", None)
        _apply_merge_decision(a, resolve_mergeability(
            a,
            regime=a.get("regime"),
            critic_verdict=a.get("critic_verdict"),
            terminal_required=terminal_required,
            terminal_stamp=a.get("terminal_stamp"),
            terminal=verdict,
            outlier_threshold_pct=outlier_quarantine_pct,
        ))
    # Top-level summary for the PR protocol (optional, additive).
    term_summary = {
        "verdict": verdict,
        "bench_ir_rows": rows,
        "profile_fingerprint": fp,
    }
    if stamp is not None:
        term_summary["terminal_stamp"] = dict(stamp)
    manifest["terminal"] = term_summary
    return manifest


def build_manifest(out_dir, *, terminal_result=None,
                   terminal_required: bool = False,
                   outlier_quarantine_pct: float = DEFAULT_OUTLIER_QUARANTINE_PCT,
                   terminal_source=None,
                   control_lanes=None,
                   control_bound_pct=None,
                   ) -> dict:
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

    # Optional terminal stamp (TerminalResult or dict) + optional on-disk source.
    term_verdict = term_rows = term_fp = None
    term_stamp = None
    if terminal_source is not None:
        term_stamp = build_terminal_stamp_from_source(
            terminal_source,
            control_lanes=control_lanes,
            control_bound_pct=control_bound_pct,
        )
        term_verdict = term_stamp.get("verdict")
        # Prefer full result for bench_ir_rows / fingerprint when provided.
        if terminal_result is not None:
            if hasattr(terminal_result, "to_dict"):
                td = terminal_result.to_dict()
            else:
                td = dict(terminal_result)
            term_rows = dict(td.get("bench_ir_rows") or {})
            term_fp = td.get("profile_fingerprint")
            if term_verdict is None:
                term_verdict = td.get("verdict")
        else:
            # Load rows/fp from the verified source file.
            src_doc = json.loads(Path(terminal_source).read_text())
            term_rows = dict(src_doc.get("bench_ir_rows") or {})
            term_fp = src_doc.get("profile_fingerprint")
    elif terminal_result is not None:
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
        entry_stamp = dict(term_stamp) if term_stamp is not None else None
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
            "mergeable": False,  # stamped below via resolve_mergeability
            "hypothesis": (props.get((a, cid), {}) or {}).get("hypothesis", ""),
            "patch_path": patch_path,
        }
        # Additive terminal fields only when the gate is in play (required or stamped).
        # Specs without terminal config keep the legacy entry shape byte-identical.
        if terminal_required or terminal_result is not None or term_stamp is not None:
            entry["terminal"] = term_verdict
            entry["bench_ir_rows"] = dict(term_rows or {})
            entry["profile_fingerprint"] = term_fp
            if entry_stamp is not None:
                entry["terminal_stamp"] = entry_stamp
        _apply_merge_decision(entry, resolve_mergeability(
            entry,
            regime=regime,
            critic_verdict=critic_verdict,
            terminal_required=terminal_required,
            terminal_stamp=entry_stamp,
            terminal=term_verdict,
            outlier_threshold_pct=outlier_quarantine_pct,
        ))
        accepted.append(entry)

    notes = (
        "Apply the accepted patches on baseline_ref, in `order` (they compound). "
        "accepted = correctness+speed PROVEN by the judge, NOT should-merge: only "
        "`mergeable:true` entries (byte-identical regime + critic pass"
        + (" + stamped TERMINAL_CONFIRMED" if terminal_required else "")
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
    if terminal_required or terminal_result is not None or term_stamp is not None:
        term_summary = {
            "verdict": term_verdict,
            "bench_ir_rows": dict(term_rows or {}),
            "profile_fingerprint": term_fp,
        }
        if term_stamp is not None:
            term_summary["terminal_stamp"] = dict(term_stamp)
        out["terminal"] = term_summary
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


def _resolve_outlier_quarantine_pct(args) -> float:
    """Spec field when --spec given; else DEFAULT (5.0, default-on)."""
    spath = getattr(args, "spec", None)
    if not spath:
        return DEFAULT_OUTLIER_QUARANTINE_PCT
    try:
        from . import spec as specmod
        raw = json.loads(Path(spath).read_text())
        sp = specmod.from_dict(raw)
        return float(sp.outlier_quarantine_pct)
    except Exception:
        return DEFAULT_OUTLIER_QUARANTINE_PCT


def _resolve_control_config(args):
    """(control_lanes, control_bound_pct) from --spec for lane-aware verify.

    When --spec is present, always returns a list for control_lanes (possibly
    empty) so terminal ingestion is lane-aware. Empty list means any stored
    control-* status is an error. Without --spec, returns (None, None) for
    lane-less self-consistency only (not sufficient for mergeability).
    """
    spath = getattr(args, "spec", None)
    if not spath:
        return None, None
    try:
        from . import spec as specmod
        from . import terminal as termmod
        raw = json.loads(Path(spath).read_text())
        sp = specmod.from_dict(raw)
        lanes = termmod.resolve_control_lanes(sp)
        bound = (
            termmod.resolve_control_composition_bound_pct(sp) if lanes else None)
        return lanes, bound
    except Exception:
        return None, None


def _load_terminal_file(path: Optional[str], *,
                        control_lanes=None,
                        control_bound_pct=None):
    """Load + verify a terminal.json. Returns (doc, source_path) or (None, None).

    Every ingestion of a terminal artifact recomputes the verdict from rows;
    a mismatched stored verdict is a hard error (tamper alarm). When
    `control_lanes` is provided, class is re-derived from row_key (lane-aware).
    """
    if not path:
        return None, None
    p = Path(path)
    raw = p.read_bytes()
    doc = json.loads(raw)
    from .terminal import verify_terminal_doc
    verify_terminal_doc(
        doc, control_lanes=control_lanes, control_bound_pct=control_bound_pct)
    return doc, str(p)


def verify_manifest_terminal_stamps(manifest: dict, *,
                                    warn=None,
                                    control_lanes=None,
                                    control_bound_pct=None) -> None:
    """Re-hash stamped sources when the file still exists.

    missing file → warning (via `warn`, default stderr); hash mismatch → hard
    error (raises SystemExit). Also re-runs verify_terminal_doc on the source.
    """
    from .terminal import TerminalError, verify_terminal_doc
    if warn is None:
        def warn(msg):  # noqa: A001 — local default matching print-style
            print(msg, file=sys.stderr)
    seen = set()
    for a in manifest.get("accepted") or []:
        stamp = a.get("terminal_stamp")
        if not isinstance(stamp, dict):
            continue
        src = stamp.get("source")
        if not src or src in seen:
            continue
        seen.add(src)
        sp = Path(src)
        if not sp.is_file():
            warn(f"warning: terminal_stamp source missing: {src}")
            continue
        actual = terminal_file_sha256(sp)
        expected = stamp.get("sha256")
        if actual != expected:
            raise SystemExit(
                f"terminal_stamp hash mismatch for {src}: "
                f"manifest={expected} file={actual}")
        try:
            verify_terminal_doc(
                json.loads(sp.read_text()),
                control_lanes=control_lanes,
                control_bound_pct=control_bound_pct,
            )
        except TerminalError as e:
            raise SystemExit(f"terminal_stamp source failed verify: {src}: {e}")


def cli(args) -> None:
    out_dir = Path(args.out_dir)
    terminal_required = _resolve_terminal_required(args)
    outlier_pct = _resolve_outlier_quarantine_pct(args)
    control_lanes, control_bound_pct = _resolve_control_config(args)
    terminal_result, terminal_source = _load_terminal_file(
        getattr(args, "terminal", None),
        control_lanes=control_lanes,
        control_bound_pct=control_bound_pct,
    )
    # Auto-load <out_dir>/terminal.json when present: any stamp widens accepted
    # entry shape (terminal/bench_ir_rows/profile_fingerprint), so non-terminal
    # specs must not leave a stray terminal.json in the run dir.
    if terminal_result is None:
        auto = out_dir / "terminal.json"
        if auto.exists():
            terminal_result, terminal_source = _load_terminal_file(
                str(auto),
                control_lanes=control_lanes,
                control_bound_pct=control_bound_pct,
            )
    m = build_manifest(out_dir, terminal_result=terminal_result,
                       terminal_required=terminal_required,
                       outlier_quarantine_pct=outlier_pct,
                       terminal_source=terminal_source,
                       control_lanes=control_lanes,
                       control_bound_pct=control_bound_pct)
    # When stamped source files still exist, re-hash (missing → warn; mismatch → die).
    verify_manifest_terminal_stamps(
        m, control_lanes=control_lanes, control_bound_pct=control_bound_pct)
    out = args.out or str(out_dir / "manifest.json")
    Path(out).write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
    n = len(m["accepted"])
    ok = sum(1 for a in m["accepted"] if a["mergeable"])
    print(f"manifest → {out}")
    gate = "byte-identical + critic pass" + (
        " + stamped TERMINAL_CONFIRMED" if terminal_required else "")
    print(f"  {n} accepted edit(s) · {ok} mergeable ({gate}) · "
          f"{n - ok} need human review")
    for a in m["accepted"]:
        flag = status_flag(a)
        d = f"{a['delta_pct']:+.2f}%" if a["delta_pct"] is not None else "?"
        term = f" terminal={a['terminal']}" if "terminal" in a else ""
        if a.get("terminal_stamp"):
            term += f" stamp={a['terminal_stamp'].get('verdict')}"
        print(f"  [{flag}] {a['attempt']} {a['fn']} {d} ({a['regime']}/"
              f"critic={a['critic_verdict']}{term}) → {a['files']}")


if __name__ == "__main__":
    from .cli import main as _cli_main
    _cli_main(["manifest"] + sys.argv[1:])
