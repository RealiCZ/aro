"""`aro recheck debts` — cheap Ir-gate re-adjudication of historical open debts.

After the instruction-count gate lands, noise-limited / no-attempt /
no-candidate / no-coverage nodes in `permtree.open_debts()` can be settled with
a single deterministic Ir A/B (plan §6.4). This module is the wiring:

  1. Walk open debts for a target spec.
  2. Recover the stored patch from the debt's `events` pointer
     (`.aro-runs/<run>#aN` → `<run>/aN/patches/`), when present.
  3. Re-evaluate through the icount gate path (build + Ir A/B).
  4. Write the result back through the normal permtree/lessons record path so
     `refuted-by-icount` / `accepted-ir` land in the ledger with fingerprints.
  5. When no patch is recoverable, emit a regenerate line for the operator —
     do not invent a closed verdict.

Fully testable with a mocked target/evaluate (valgrind is not required on
macOS); the real campaign run is server-side.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from . import lessons as lessonsmod
from . import patchfile
from . import permtree
from .types import Candidate, Patch, Verdict, best_improvement


# Terminal Ir outcomes that close a historical noise-limited claim as refuted
# (no product difference under production codegen / Ir). ACCEPTED_IR keeps its
# own verdict — a true win that the wall-clock floor previously buried.
_REFUTE_VERDICTS = {
    Verdict.NEUTRAL_IR.value,
    Verdict.REGRESSED_IR.value,
    Verdict.WITHIN_NOISE.value,
    Verdict.REGRESSED.value,
    Verdict.NOISE_LIMITED.value,  # still noise-limited after Ir path → no proof
}


def parse_events_ref(ref: str, *, runs_root: Optional[Path] = None) -> Optional[Path]:
    """Map a permtree `events` pointer to an attempt directory on disk.

    Conventions observed in the ledger:
      `.aro-runs/mega-evm-0703#a14`  →  `.aro-runs/mega-evm-0703/a14`
      `.aro-runs/mega-evm-explore`   →  `.aro-runs/mega-evm-explore`
    Returns None when the pointer is empty or the path does not exist.
    """
    if not ref or not isinstance(ref, str):
        return None
    ref = ref.strip()
    if not ref:
        return None
    if "#" in ref:
        base, att = ref.rsplit("#", 1)
        path = Path(base) / att
    else:
        path = Path(ref)
    if runs_root is not None and not path.is_absolute():
        # Allow tests to root relative .aro-runs under a temp dir.
        cand = runs_root / path
        if cand.exists():
            return cand
    return path if path.exists() else None


def find_patch_for_debt(debt: dict, attempt_dir: Path) -> Optional[Path]:
    """Locate a recoverable patch file for one open-debt row.

    Prefers a records.jsonl row whose hypothesis matches the debt's, else the
    first non-base agent patch in `patches/`. Returns None when nothing useful
    is on disk (operator must regenerate).
    """
    patches_dir = attempt_dir / "patches"
    if not patches_dir.is_dir():
        return None
    records_path = attempt_dir / "records.jsonl"
    hyp = (debt.get("hypothesis") or "").strip()
    if records_path.exists() and hyp:
        try:
            for ln in records_path.read_text().splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except Exception:
                    continue
                rh = (row.get("hypothesis") or "").strip()
                if not rh:
                    continue
                # Prefix match: permtree truncates hypothesis to 400 chars.
                if rh.startswith(hyp[:80]) or hyp.startswith(rh[:80]):
                    pid = row.get("id") or ""
                    if pid and pid != "base-0":
                        pf = patches_dir / (patchfile.safe_id(pid) + ".txt")
                        if pf.exists() and pf.read_text().strip() not in ("", "NoOp"):
                            return pf
        except Exception:
            pass
    # Fallback: first non-NoOp patch that is not the base seed.
    for pf in sorted(patches_dir.glob("*.txt")):
        if pf.stem in ("base-0", "base_0"):
            continue
        text = pf.read_text()
        if text.strip() and text.strip() != "NoOp":
            return pf
    return None


def recover_candidate(debt: dict, *, runs_root: Optional[Path] = None
                      ) -> tuple[Optional[Candidate], Optional[Path], str]:
    """Try to rebuild a Candidate from the debt's events pointer.

    Returns `(candidate_or_None, patch_path_or_None, status_note)`.
    """
    ref = debt.get("events") or debt.get("events_ref") or ""
    attempt = parse_events_ref(ref, runs_root=runs_root)
    if attempt is None:
        return None, None, (f"no recoverable run dir for events={ref!r} — "
                            f"regenerate the candidate for {debt.get('fn')}")
    pf = find_patch_for_debt(debt, attempt)
    if pf is None:
        return None, None, (f"run dir {attempt} has no recoverable patch — "
                            f"regenerate the candidate for {debt.get('fn')}")
    edits = patchfile.parse(pf.read_text())
    if not edits:
        return None, pf, (f"patch {pf} is empty/NoOp — regenerate for {debt.get('fn')}")
    cid = f"recheck-{debt.get('fn', 'x')}-{pf.stem}"
    hyp = debt.get("hypothesis") or f"recheck of {debt.get('fn')}"
    return Candidate(id=cid, hypothesis=hyp, patch=Patch(edits=edits)), pf, "ok"


def map_outcome_verdict(outcome_verdict: str) -> str:
    """Debt-recheck ledger mapping: terminal non-wins become refuted-by-icount
    so open_debts() clears them; true Ir wins keep accepted-ir."""
    if outcome_verdict == Verdict.ACCEPTED_IR.value:
        return Verdict.ACCEPTED_IR.value
    if outcome_verdict == Verdict.ACCEPTED.value:
        # locality passthrough wall-clock accept — rare on debt recheck; keep
        return Verdict.ACCEPTED.value
    if outcome_verdict in _REFUTE_VERDICTS or outcome_verdict in (
            "neutral-ir", "regressed-ir", "within-noise", "regressed",
            "noise-limited"):
        return Verdict.REFUTED_BY_ICOUNT.value
    # build/verify/no-coverage/rejected: record as-is (no silent close)
    return outcome_verdict


def recheck_one(spec, debt: dict, *, target=None, evaluate_fn: Optional[Callable] = None,
                runs_root: Optional[Path] = None, write: bool = True,
                floors=None, objectives=None, ab_pairs: int = 2) -> dict:
    """Re-adjudicate one open-debt row. Returns a result dict.

    `evaluate_fn`, when provided, short-circuits the real judge (selftest path):
        evaluate_fn(candidate) -> EvalOutcome
    Otherwise builds worktrees via `target` and calls `eval.evaluate`.
    """
    fn = debt.get("fn") or "?"
    wl = debt.get("workload") or getattr(spec, "name", "?")
    base = debt.get("base") or debt.get("base_state") or "origin"
    out = {"fn": fn, "workload": wl, "base": base, "key": debt.get("key"),
           "prior_verdict": debt.get("verdict"), "status": "?", "verdict": None,
           "note": "", "patch": None, "ir_delta_pct": None,
           "profile_fingerprint": None}

    cand, pf, note = recover_candidate(debt, runs_root=runs_root)
    if cand is None:
        out["status"] = "regenerate"
        out["note"] = note
        return out
    out["patch"] = str(pf)

    if evaluate_fn is not None:
        outcome = evaluate_fn(cand)
    else:
        if target is None:
            out["status"] = "error"
            out["note"] = "no target and no evaluate_fn — cannot measure"
            return out
        from . import eval as evalmod
        from .types import NoiseFloors, Objective
        objs = objectives or [Objective(m, True) for m in
                              (getattr(spec, "metric_names", None) or ["ns_per_call"])]
        fl = floors or NoiseFloors()
        try:
            baseline = target.make_worktree("recheck-base")
        except Exception as e:
            out["status"] = "error"
            out["note"] = f"baseline worktree failed: {e}"
            return out
        try:
            outcome = evalmod.evaluate(
                target, baseline, Patch.noop(), cand, ab_pairs, fl, objs,
                aa_runs=1, bench_scales=(1,))
        except Exception as e:
            out["status"] = "error"
            out["note"] = f"evaluate failed: {e}"
            return out
        finally:
            try:
                target.remove_worktree(baseline)
            except Exception:
                pass

    raw_v = outcome.verdict.value if hasattr(outcome.verdict, "value") else str(outcome.verdict)
    ledger_v = map_outcome_verdict(raw_v)
    ir_d = getattr(outcome, "ir_delta_pct", None)
    fp = getattr(outcome, "profile_fingerprint", None)
    env_fp = getattr(outcome, "env_fingerprint", None)
    note_txt = (outcome.notes[-1] if getattr(outcome, "notes", None) else "") or ""
    if ledger_v == Verdict.REFUTED_BY_ICOUNT.value and raw_v != ledger_v:
        note_txt = (f"refuted-by-icount (raw={raw_v}): historical debt closed by "
                    f"Ir gate — {note_txt}")[:240]

    out["status"] = "rechecked"
    out["verdict"] = ledger_v
    out["raw_verdict"] = raw_v
    out["note"] = note_txt
    out["ir_delta_pct"] = ir_d
    out["profile_fingerprint"] = fp
    out["env_fingerprint"] = env_fp

    if write:
        # Best Δ for lessons delta_pct (Ir preferred when present).
        minz = {o.metric: True for o in (objectives or [])} or {"Ir": True}
        b = best_improvement(getattr(outcome, "deltas", None) or [], minz)
        delta = (ir_d if isinstance(ir_d, (int, float))
                 else (b[0].delta_pct if b else debt.get("delta")))
        permtree.record(
            getattr(spec, "name", wl),
            workload=wl, fn=fn, base_state=base,
            verdict=ledger_v, regime=debt.get("regime") or "strict",
            delta=delta, pct=debt.get("pct"), files=debt.get("files") or [],
            hypothesis=debt.get("hypothesis") or cand.hypothesis,
            events_ref=debt.get("events") or "",
            run_id="recheck-debts",
            ir_delta_pct=ir_d, profile_fingerprint=fp,
            env_fingerprint=env_fp)
        lessonsmod.append(
            getattr(spec, "name", wl),
            cand.hypothesis, ledger_v, delta,
            note_txt, ir_delta_pct=ir_d, profile_fingerprint=fp,
            env_fingerprint=env_fp,
            baseline_sha=getattr(spec, "baseline_ref", None),
            repo=str(getattr(spec, "repo", "") or ""))
    return out


def recheck_debts(spec, *, target=None, evaluate_fn: Optional[Callable] = None,
                  runs_root: Optional[Path] = None, write: bool = True,
                  floors=None, objectives=None) -> list:
    """Walk open debts for `spec` and recheck each. Returns list of result dicts."""
    name = getattr(spec, "name", None) or str(spec)
    rows = permtree.load(name)
    debts = permtree.open_debts(rows)
    # Prefer debts for this workload; fall back to all open rows in the ledger.
    mine = [d for d in debts if d.get("workload") in (name, None, "")]
    if not mine:
        mine = list(debts)
    results = []
    for d in mine:
        results.append(recheck_one(
            spec, d, target=target, evaluate_fn=evaluate_fn,
            runs_root=runs_root, write=write, floors=floors,
            objectives=objectives))
    return results


def cli(args) -> None:
    from . import spec as specmod
    from .target import SpecTarget

    sp = specmod.load(args.spec)
    # list-only must never construct SpecTarget: that mkdirs/resolves the
    # server-only target checkout. Handle (and return) before any target touch.
    if args.list_only:
        rows = permtree.load(sp.name)
        debts = permtree.open_debts(rows)
        print(f"open debts for {sp.name}: {len(debts)}")
        for d in debts:
            cand, pf, note = recover_candidate(d)
            tag = f"patch={pf}" if pf else f"REGENERATE ({note})"
            print(f"  {d.get('key') or d.get('fn')}: {d.get('verdict')}  {tag}")
        return

    write = not args.dry_run
    target = SpecTarget(sp)
    results = recheck_debts(sp, target=target, write=write,
                            runs_root=Path(args.runs_root) if args.runs_root else None)
    n_ok = sum(1 for r in results if r["status"] == "rechecked")
    n_reg = sum(1 for r in results if r["status"] == "regenerate")
    n_err = sum(1 for r in results if r["status"] == "error")
    print(f"recheck debts {sp.name}: {len(results)} debt(s) — "
          f"{n_ok} rechecked, {n_reg} regenerate, {n_err} error"
          + (" [dry-run, no writes]" if not write else ""))
    for r in results:
        v = r.get("verdict") or r["status"]
        print(f"  {r['fn']}: {v} — {r.get('note', '')[:160]}")
        if r["status"] == "regenerate":
            print(f"    → regenerate candidate for `{r['fn']}` "
                  f"(no stored patch under events pointer)")
