"""`aro certify` — candidates → stamped manifest (decision table executable).

Orchestrates the certification state machine:

  recheck candidates → terminal measure → verdict dispatch
    → (MIXED only) greedy attribution prune ≤2 rounds → re-terminal
    → stamp via existing apply_terminal / rejudge path

Gate math (floors, hysteresis, trade cap, control bound, reverify chain) stays
in the underlying modules. This module only walks the decision table and logs
every prune drop with ablate attribution evidence.

Injectable stage callables (``recheck_fn``, ``terminal_fn``, ``ablate_fn``,
``stamp_fn``) keep tests hermetic; production defaults are thin adapters.

Exit codes: 0 stamped · 2 work-order stop · 1 error.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .reverify import VERDICT_PASS, parse_orders
from .terminal import (
    TERMINAL_CONFIRMED,
    TERMINAL_CONFIRMED_WITH_TRADE,
    TERMINAL_CONTROL_ANOMALY,
    TERMINAL_MIXED,
    TERMINAL_REGRESSED,
    TERMINAL_TEST_FAILED,
    TERMINAL_UNTOUCHED,
    TERMINAL_VERDICT_META,
    classify_subject_regression,
    resolve_protected_hysteresis,
    resolve_protected_row_families,
    resolve_tradeable_regression_cap_pct,
)

# --- constants ----------------------------------------------------------------

STAGES = ("recheck", "terminal", "prune", "stamp")
MAX_PRUNE_ROUNDS = 2
PRUNE_LEDGER_NAME = "certify-prune.jsonl"
REVERIFY_DOC_NAME = "reverify.json"

EXIT_STAMPED = 0
EXIT_ERROR = 1
EXIT_WORK_ORDER = 2

# Decision-table next actions (must match OPERATIONS §13.6 wording intent).
_DECISION_NEXT = {
    TERMINAL_CONFIRMED: "stamp (`--update-manifest`) → run-to-pr",
    TERMINAL_CONFIRMED_WITH_TRADE: (
        "stamp → run-to-pr; PR body MUST list every traded regression "
        "(row, Δ%, cap)"
    ),
    TERMINAL_MIXED: (
        "greedy attribution-based prune (≤2 rounds) → re-terminal → "
        "re-enter this table; CONTROL_ANOMALY never pruned"
    ),
    TERMINAL_REGRESSED: (
        "no PR; record the terminal doc; candidates stay non-mergeable; "
        "close out with a report"
    ),
    TERMINAL_UNTOUCHED: (
        "no PR (criterion rows did not move); candidates go to the frozen / "
        "sub-resolution pool per the standing instrument protocol"
    ),
    TERMINAL_TEST_FAILED: (
        "drop the offending entry (recheck `--apply` demotes it), re-run "
        "terminal on the remaining set → re-enter this table"
    ),
    TERMINAL_CONTROL_ANOMALY: (
        "run the A/A disambiguation protocol FIRST; never touch "
        "`control_composition_bound_pct` on your own — escalate WITH the "
        "A/A evidence attached"
    ),
}


# --- result / errors ----------------------------------------------------------

@dataclass
class CertifyResult:
    """Outcome of one certify run (also carries work-order context)."""

    exit_code: int
    verdict: Optional[str] = None
    measured_orders: list = field(default_factory=list)
    message: str = ""
    stamped: bool = False
    prune_rounds: int = 0
    terminal_path: Optional[Path] = None
    violations: list = field(default_factory=list)


class CertifyError(Exception):
    """Hard error — environment / usage / missing artifact (exit 1)."""


class CertifyStop(Exception):
    """Prescribed work-order stop (exit 2)."""

    def __init__(self, message: str, *, verdict: Optional[str] = None,
                 next_action: Optional[str] = None,
                 violations: Optional[list] = None):
        super().__init__(message)
        self.message = message
        self.verdict = verdict
        self.next_action = next_action
        self.violations = list(violations or [])


# --- artifact helpers ---------------------------------------------------------

def terminal_artifact_path(out_dir, round_n: int) -> Path:
    return Path(out_dir) / f"terminal-c{round_n}.json"


def prune_ledger_path(out_dir) -> Path:
    return Path(out_dir) / PRUNE_LEDGER_NAME


def latest_terminal_round(out_dir) -> Optional[int]:
    """Highest N for which terminal-cN.json exists, or None."""
    out = Path(out_dir)
    best = None
    for p in out.glob("terminal-c*.json"):
        name = p.name  # terminal-c12.json
        if not name.startswith("terminal-c") or not name.endswith(".json"):
            continue
        mid = name[len("terminal-c"):-len(".json")]
        if mid.isdigit():
            n = int(mid)
            if best is None or n > best:
                best = n
    return best


def load_terminal_doc(path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise CertifyError(f"missing terminal artifact: {p}")
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CertifyError(f"failed to read {p}: {e}") from e
    if not isinstance(doc, dict):
        raise CertifyError(f"terminal artifact is not a JSON object: {p}")
    return doc


def load_reverify_doc(out_dir) -> dict:
    p = Path(out_dir) / REVERIFY_DOC_NAME
    if not p.is_file():
        raise CertifyError(
            f"missing {REVERIFY_DOC_NAME} in {out_dir} "
            f"(run recheck stage first, or pass --from recheck)")
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CertifyError(f"failed to read {p}: {e}") from e
    return doc


def survivors_from_reverify(doc: dict, orders_filter=None) -> list:
    """1-based orders with reverify-pass, optionally intersected with filter."""
    filt = None
    if orders_filter is not None:
        filt = (orders_filter if isinstance(orders_filter, (set, frozenset))
                else set(orders_filter))
    out = []
    for e in doc.get("entries") or []:
        if e.get("verdict") != VERDICT_PASS:
            continue
        order = e.get("order")
        if order is None:
            continue
        o = int(order)
        if filt is not None and o not in filt:
            continue
        out.append(o)
    return sorted(out)


def _fn_for_order(out_dir, order: int) -> str:
    man = Path(out_dir) / "manifest.json"
    if not man.is_file():
        return ""
    try:
        m = json.loads(man.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    for a in m.get("accepted") or []:
        if int(a.get("order") or 0) == int(order):
            return str(a.get("fn") or "")
    return ""


# --- violation + greedy prune -------------------------------------------------

def collect_violations(terminal_doc: dict, *, spec=None,
                       protected_families=None,
                       cap_pct=None,
                       hysteresis=None) -> list:
    """Subject-row violations under row-family policy (no gate reimplementation).

    Uses ``classify_subject_regression`` — protected over floor+hysteresis
    (zero tolerance beyond H) and tradeable over the trade cap.
    Returns list of ``{row_key, delta_pct, floor_pct, kind}``.
    """
    if protected_families is None and spec is not None:
        protected_families = resolve_protected_row_families(spec)
    if cap_pct is None and spec is not None:
        cap_pct = resolve_tradeable_regression_cap_pct(spec)
    if hysteresis is None and spec is not None:
        hysteresis = resolve_protected_hysteresis(spec)

    families = [str(x) for x in (protected_families or [])]
    cap = float(cap_pct) if cap_pct is not None else 0.0
    out = []
    for r in terminal_doc.get("rows") or []:
        if not isinstance(r, dict):
            continue
        st = str(r.get("status") or "")
        if st != "regressed":
            continue
        kind = classify_subject_regression(
            r, protected_families=families, cap_pct=cap,
            hysteresis=hysteresis)
        if kind != "violation":
            continue
        out.append({
            "row_key": str(r.get("row_key") or ""),
            "delta_pct": float(r.get("delta_pct") or 0.0),
            "floor_pct": float(r.get("floor_pct") or 0.0),
            "kind": kind,
        })
    return out


def max_contribution_order(ablate_doc: dict, row_key: str,
                           orders) -> tuple:
    """Entry order with the largest positive marginal Δ% on *row_key*.

    Returns ``(order, delta_pct)`` or ``(None, None)`` when attribution is empty.
    """
    order_set = None
    if orders is not None:
        order_set = (orders if isinstance(orders, (set, frozenset))
                     else set(int(x) for x in orders))
    best_order = None
    best_dp = None
    for e in ablate_doc.get("entries") or []:
        o = e.get("order")
        if o is None:
            continue
        o = int(o)
        if order_set is not None and o not in order_set:
            continue
        summary = e.get("marginal_rows_summary") or {}
        if row_key not in summary:
            continue
        try:
            dp = float(summary[row_key])
        except (TypeError, ValueError):
            continue
        if best_dp is None or dp > best_dp:
            best_dp = dp
            best_order = o
    return best_order, best_dp


def append_prune_ledger(out_dir, records: list) -> Path:
    """Append drop records as JSONL; create file if missing."""
    path = prune_ledger_path(out_dir)
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def greedy_prune_orders(spec, out_dir, orders, terminal_doc: dict,
                        ablate_doc: dict, *, round_n: int) -> tuple:
    """Drop max-contribution entry per violated row; log every drop.

    Returns ``(new_orders_sorted, drop_records)``.
    Raises CertifyStop when attribution is incomplete for a violation.
    """
    violations = collect_violations(terminal_doc, spec=spec)
    if not violations:
        return sorted(int(x) for x in orders), []

    order_set = set(int(x) for x in orders)
    drops: dict = {}  # order -> record (dedupe multi-row same entry)
    for v in violations:
        row_key = v["row_key"]
        order, atr_dp = max_contribution_order(ablate_doc, row_key, order_set)
        if order is None:
            raise CertifyStop(
                f"evidence incomplete: cannot attribute violated row "
                f"{row_key!r} (Δ={v['delta_pct']:+.4f}%) to any measured entry "
                f"— stop; re-run ablate or inspect terminal-c{round_n}.json",
                verdict=TERMINAL_MIXED,
                next_action=_DECISION_NEXT[TERMINAL_MIXED],
                violations=violations,
            )
        if order not in order_set:
            continue
        if order in drops:
            # Already dropping this entry; note extra violated row in ledger only.
            continue
        evidence = (
            f"ablate.json#order={order}:marginal_rows_summary[{row_key}]"
            f"={atr_dp:+.4f}%"
        )
        drops[order] = {
            "round": int(round_n),
            "dropped_order": int(order),
            "fn": _fn_for_order(out_dir, order),
            "violated_row": row_key,
            "delta_pct": float(v["delta_pct"]),
            "evidence": evidence,
        }

    if not drops:
        raise CertifyStop(
            "MIXED but prune produced no drops (attribution empty) — stop",
            verdict=TERMINAL_MIXED,
            next_action=_DECISION_NEXT[TERMINAL_MIXED],
            violations=violations,
        )

    records = [drops[o] for o in sorted(drops)]
    append_prune_ledger(out_dir, records)
    new_orders = sorted(o for o in order_set if o not in drops)
    return new_orders, records


# --- production stage adapters ------------------------------------------------

def _build_measure_worktrees(spec, out_dir, orders, *, target=None):
    """Pristine baseline + candidate with *orders* applied in chain order.

    Infrastructure only — uses the same apply/patch helpers as reverify/ablate.
    Caller must remove worktrees.
    """
    from .reverify import load_entry_patch
    from .target import SpecTarget

    if target is None:
        target = SpecTarget(spec)
    out_dir = Path(out_dir)
    man = json.loads((out_dir / "manifest.json").read_text())
    accepted = sorted(man.get("accepted") or [],
                      key=lambda e: e.get("order") or 0)
    order_set = set(int(x) for x in (orders or []))

    baseline = target.make_worktree("certify-base")
    candidate = target.make_worktree("certify-cand")
    try:
        for entry in accepted:
            o = entry.get("order")
            if o is None or int(o) not in order_set:
                # Still apply earlier chain members when they are in the set
                # only — compounding for the measured shipping set is by
                # applying measured orders in order (not skipped prefixes).
                # Matches terminal --orders membership: only measured patches
                # are on the candidate tree for the stamp set.
                continue
            patch = load_entry_patch(out_dir, entry)
            target.apply(patch, candidate)
    except Exception:
        try:
            target.remove_worktree(candidate)
        except Exception:
            pass
        try:
            target.remove_worktree(baseline)
        except Exception:
            pass
        raise
    return baseline, candidate, target


def default_recheck_fn(spec, out_dir, orders):
    """Adapter: ``reverify.reverify`` + survivor list (+ measure worktrees)."""
    from .reverify import PREFLIGHT_FAIL_MSG, reverify

    doc = reverify(spec, out_dir, orders=orders, apply=True)
    if doc.get("preflight") == "fail":
        return {
            "preflight": "fail",
            "detail": doc.get("detail") or PREFLIGHT_FAIL_MSG,
            "survivors": [],
            "doc": doc,
            "baseline_dir": None,
            "candidate_dir": None,
            "target": None,
        }
    survivors = survivors_from_reverify(doc, orders)
    baseline_dir = candidate_dir = target = None
    if survivors:
        baseline_dir, candidate_dir, target = _build_measure_worktrees(
            spec, out_dir, survivors)
    return {
        "preflight": "pass",
        "detail": "",
        "survivors": survivors,
        "doc": doc,
        "baseline_dir": baseline_dir,
        "candidate_dir": candidate_dir,
        "target": target,
    }


def default_terminal_fn(spec, out_dir, orders, round_n, *,
                        baseline_dir=None, candidate_dir=None, target=None):
    """Adapter: ``run_terminal`` → write ``terminal-c<N>.json``."""
    from .terminal import (
        run_terminal,
        resolve_measure_baseline_sha,
        terminal_doc_dict,
    )

    owned = False
    if baseline_dir is None or candidate_dir is None:
        baseline_dir, candidate_dir, target = _build_measure_worktrees(
            spec, out_dir, orders, target=target)
        owned = True
    try:
        result = run_terminal(spec, baseline_dir, candidate_dir)
        bsha = resolve_measure_baseline_sha(spec, baseline_dir)
        order_list = sorted(int(x) for x in orders) if orders is not None else None
        doc = terminal_doc_dict(
            result, measured_orders=order_list, baseline_sha=bsha)
        path = terminal_artifact_path(out_dir, round_n)
        Path(path).write_text(
            json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        return {
            "verdict": result.verdict,
            "path": Path(path),
            "doc": doc,
        }
    finally:
        if owned and target is not None:
            try:
                target.remove_worktree(candidate_dir)
            except Exception:
                pass
            try:
                target.remove_worktree(baseline_dir)
            except Exception:
                pass


def default_ablate_fn(spec, out_dir, orders):
    """Adapter: ``ablate.ablate`` (proposal + marginal attribution)."""
    from .ablate import ablate

    return ablate(spec, out_dir, orders=orders)


def default_stamp_fn(spec, out_dir, terminal_path, orders):
    """Adapter: stamp via ``apply_terminal`` with explicit measured orders.

    Same write-back path as ``aro terminal --rejudge --update-manifest --orders``.
    Never invents stamps; membership keeps dropped entries TERMINAL_NOT_MEASURED.
    """
    from . import manifest as manifestmod
    from .terminal import (
        has_terminal_config,
        resolve_control_composition_bound_pct,
        resolve_control_lanes,
        resolve_protected_hysteresis,
        resolve_protected_row_families,
        resolve_tradeable_regression_cap_pct,
    )

    out_dir = Path(out_dir)
    man_path = out_dir / "manifest.json"
    if not man_path.is_file():
        raise CertifyError(f"no manifest.json in {out_dir}")
    terminal_path = Path(terminal_path)
    doc = load_terminal_doc(terminal_path)

    lanes = resolve_control_lanes(spec)
    bound = resolve_control_composition_bound_pct(spec) if lanes else None
    families = resolve_protected_row_families(spec)
    cap = resolve_tradeable_regression_cap_pct(spec) if families else None
    hyst = resolve_protected_hysteresis(spec) if families else None
    oq = float(getattr(
        spec, "outlier_quarantine_pct",
        manifestmod.DEFAULT_OUTLIER_QUARANTINE_PCT))
    order_set = None
    if orders is not None:
        order_set = {int(x) for x in orders}

    m = json.loads(man_path.read_text())
    m = manifestmod.apply_terminal(
        m, doc,
        terminal_required=has_terminal_config(spec),
        outlier_quarantine_pct=oq,
        source=str(terminal_path),
        control_lanes=lanes,
        control_bound_pct=bound,
        protected_row_families=families or None,
        tradeable_regression_cap_pct=cap,
        protected_hysteresis=hyst,
        orders=order_set,
    )
    man_path.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
    return m


def _print_mergeable_table(manifest: dict) -> None:
    accepted = manifest.get("accepted") or []
    print(f"{'order':>5}  {'id':<24}  {'fn':<20}  {'terminal':<28}  mergeable")
    for a in sorted(accepted, key=lambda e: e.get("order") or 0):
        print(f"{a.get('order') or '?':>5}  "
              f"{str(a.get('id') or ''):<24}  "
              f"{str(a.get('fn') or ''):<20}  "
              f"{str(a.get('terminal') or ''):<28}  "
              f"{bool(a.get('mergeable'))}")
    n_ok = sum(1 for a in accepted if a.get("mergeable"))
    print(f"  {n_ok}/{len(accepted)} mergeable")


def _decision_next(verdict: str) -> str:
    if verdict in _DECISION_NEXT:
        return _DECISION_NEXT[verdict]
    meta = TERMINAL_VERDICT_META.get(str(verdict or "")) or {}
    return meta.get("next") or f"stop — unhandled verdict {verdict}"


# --- orchestrator -------------------------------------------------------------

def certify(spec, out_dir, *,
            orders=None,
            from_stage: str = "recheck",
            recheck_fn: Optional[Callable] = None,
            terminal_fn: Optional[Callable] = None,
            ablate_fn: Optional[Callable] = None,
            stamp_fn: Optional[Callable] = None,
            max_prune_rounds: int = MAX_PRUNE_ROUNDS) -> CertifyResult:
    """Run the certification state machine.

    Parameters
    ----------
    spec : TargetSpec
    out_dir : campaign run dir (manifest.json + patches)
    orders : optional set/list/str of 1-based orders (same parser as recheck)
    from_stage : recheck | terminal | prune | stamp — surgical re-entry
    recheck_fn / terminal_fn / ablate_fn / stamp_fn : injectable stage adapters
    max_prune_rounds : hard cap on MIXED prune iterations (default 2)

    Returns CertifyResult with exit_code 0/1/2.
    """
    out_dir = Path(out_dir)
    if from_stage not in STAGES:
        raise CertifyError(
            f"invalid --from {from_stage!r}; choose one of {', '.join(STAGES)}")

    order_filter = orders
    if isinstance(orders, str) or orders is None:
        order_filter = parse_orders(orders)
    elif not isinstance(orders, (set, frozenset)):
        order_filter = {int(x) for x in orders}

    recheck_fn = recheck_fn or default_recheck_fn
    terminal_fn = terminal_fn or default_terminal_fn
    ablate_fn = ablate_fn or default_ablate_fn
    stamp_fn = stamp_fn or default_stamp_fn

    baseline_dir = candidate_dir = target = None
    owned_worktrees = False
    measured: list = []
    prune_rounds_done = 0
    terminal_path: Optional[Path] = None
    terminal_doc: Optional[dict] = None
    round_n = 1

    try:
        # ----- recheck --------------------------------------------------------
        if from_stage == "recheck":
            rc = recheck_fn(spec, out_dir, order_filter)
            if rc.get("preflight") == "fail":
                detail = rc.get("detail") or "recheck preflight failed"
                print(f"certify STOP (error): {detail}", file=sys.stderr)
                return CertifyResult(
                    exit_code=EXIT_ERROR,
                    message=detail,
                )
            measured = list(rc.get("survivors") or [])
            baseline_dir = rc.get("baseline_dir")
            candidate_dir = rc.get("candidate_dir")
            target = rc.get("target")
            owned_worktrees = baseline_dir is not None
            if not measured:
                msg = ("recheck produced zero reverify-pass survivors — "
                       "nothing to certify")
                print(f"certify STOP (work order): {msg}", file=sys.stderr)
                return CertifyResult(
                    exit_code=EXIT_WORK_ORDER,
                    message=msg,
                    measured_orders=[],
                )
            print(f"certify recheck: {len(measured)} survivor(s) "
                  f"{measured}")
        elif from_stage in ("terminal", "prune", "stamp"):
            # Reuse reverify survivors when present; else --orders / all.
            rev_path = out_dir / REVERIFY_DOC_NAME
            if rev_path.is_file():
                rev = load_reverify_doc(out_dir)
                if rev.get("preflight") == "fail":
                    detail = rev.get("detail") or "recheck preflight failed"
                    return CertifyResult(
                        exit_code=EXIT_ERROR, message=detail)
                measured = survivors_from_reverify(rev, order_filter)
            elif order_filter is not None:
                measured = sorted(int(x) for x in order_filter)
            else:
                # Fall back to all accepted orders from the manifest.
                man_path = out_dir / "manifest.json"
                if not man_path.is_file():
                    raise CertifyError(f"no manifest.json in {out_dir}")
                man = json.loads(man_path.read_text())
                measured = sorted(
                    int(a["order"]) for a in (man.get("accepted") or [])
                    if a.get("order") is not None)

        # ----- stamp-only re-entry --------------------------------------------
        if from_stage == "stamp":
            n = latest_terminal_round(out_dir)
            if n is None:
                raise CertifyError(
                    "no terminal-cN.json in run dir for --from stamp")
            terminal_path = terminal_artifact_path(out_dir, n)
            terminal_doc = load_terminal_doc(terminal_path)
            verdict = str(terminal_doc.get("verdict") or "")
            mo = terminal_doc.get("measured_orders")
            if mo is not None:
                measured = sorted(int(x) for x in mo)
            if verdict not in (TERMINAL_CONFIRMED, TERMINAL_CONFIRMED_WITH_TRADE):
                raise CertifyStop(
                    f"cannot stamp verdict {verdict}: "
                    f"{_decision_next(verdict)}",
                    verdict=verdict,
                    next_action=_decision_next(verdict),
                )
            man = stamp_fn(spec, out_dir, terminal_path, measured)
            _print_mergeable_table(man if isinstance(man, dict) else
                                   json.loads((out_dir / "manifest.json")
                                              .read_text()))
            print(f"certify STAMPED ({verdict}) orders={measured} "
                  f"← {terminal_path}")
            return CertifyResult(
                exit_code=EXIT_STAMPED,
                verdict=verdict,
                measured_orders=list(measured),
                message=f"stamped {verdict}",
                stamped=True,
                terminal_path=Path(terminal_path),
            )

        # ----- prune re-entry: load latest terminal, enter dispatch -----------
        if from_stage == "prune":
            n = latest_terminal_round(out_dir)
            if n is None:
                raise CertifyError(
                    "no terminal-cN.json in run dir for --from prune")
            round_n = n
            terminal_path = terminal_artifact_path(out_dir, n)
            terminal_doc = load_terminal_doc(terminal_path)
            mo = terminal_doc.get("measured_orders")
            if mo is not None:
                measured = sorted(int(x) for x in mo)
            # Count prior prune ledger lines as already-used rounds.
            ledger = prune_ledger_path(out_dir)
            if ledger.is_file():
                rounds_seen = set()
                for line in ledger.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        rounds_seen.add(int(rec.get("round") or 0))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                prune_rounds_done = len(rounds_seen)
        else:
            # terminal (or post-recheck): measure or reuse terminal-c1
            round_n = 1
            art = terminal_artifact_path(out_dir, round_n)
            if art.is_file() and from_stage == "terminal":
                # Artifact reuse on surgical re-entry.
                terminal_path = art
                terminal_doc = load_terminal_doc(art)
                mo = terminal_doc.get("measured_orders")
                if mo is not None:
                    measured = sorted(int(x) for x in mo)
                print(f"certify terminal: reusing {art}")
            else:
                if not measured:
                    raise CertifyError("no orders to measure at terminal stage")
                tr = terminal_fn(
                    spec, out_dir, measured, round_n,
                    baseline_dir=baseline_dir,
                    candidate_dir=candidate_dir,
                    target=target,
                )
                terminal_path = Path(tr["path"])
                terminal_doc = tr["doc"]
                # Worktrees may have been owned by recheck; terminal default
                # may have consumed them. Clear so finally does not double-free
                # if the adapter already cleaned owned trees.
                if tr.get("consumed_worktrees"):
                    baseline_dir = candidate_dir = None
                    owned_worktrees = False
                print(f"certify terminal-c{round_n}: {tr['verdict']} "
                      f"→ {terminal_path}")

        # ----- verdict dispatch / prune loop ----------------------------------
        assert terminal_doc is not None
        while True:
            verdict = str(terminal_doc.get("verdict") or "")

            if verdict in (TERMINAL_CONFIRMED, TERMINAL_CONFIRMED_WITH_TRADE):
                man = stamp_fn(spec, out_dir, terminal_path, measured)
                if isinstance(man, dict):
                    _print_mergeable_table(man)
                else:
                    mp = out_dir / "manifest.json"
                    if mp.is_file():
                        _print_mergeable_table(json.loads(mp.read_text()))
                print(f"certify STAMPED ({verdict}) orders={measured} "
                      f"← {terminal_path}")
                return CertifyResult(
                    exit_code=EXIT_STAMPED,
                    verdict=verdict,
                    measured_orders=list(measured),
                    message=f"stamped {verdict}",
                    stamped=True,
                    prune_rounds=prune_rounds_done,
                    terminal_path=Path(terminal_path) if terminal_path else None,
                )

            if verdict == TERMINAL_CONTROL_ANOMALY:
                nxt = _decision_next(verdict)
                msg = (
                    f"STOP: {verdict} — {nxt}\n"
                    f"(pruning must NOT run — control drift is instrumentation, "
                    f"not attribution)"
                )
                print(f"certify {msg}", file=sys.stderr)
                return CertifyResult(
                    exit_code=EXIT_WORK_ORDER,
                    verdict=verdict,
                    measured_orders=list(measured),
                    message=msg,
                    prune_rounds=prune_rounds_done,
                    terminal_path=Path(terminal_path) if terminal_path else None,
                )

            if verdict == TERMINAL_MIXED:
                if prune_rounds_done >= max_prune_rounds:
                    viols = collect_violations(terminal_doc, spec=spec)
                    ledger = prune_ledger_path(out_dir)
                    msg = (
                        f"STOP: TERMINAL_MIXED still violating after "
                        f"{max_prune_rounds} prune round(s) — "
                        f"escalate (two prune→re-terminal iterations failed "
                        f"to converge)\n"
                        f"surviving violations: "
                        f"{json.dumps(viols, ensure_ascii=False)}\n"
                        f"prune ledger: {ledger}"
                    )
                    print(f"certify {msg}", file=sys.stderr)
                    return CertifyResult(
                        exit_code=EXIT_WORK_ORDER,
                        verdict=verdict,
                        measured_orders=list(measured),
                        message=msg,
                        prune_rounds=prune_rounds_done,
                        terminal_path=Path(terminal_path) if terminal_path else None,
                        violations=viols,
                    )

                # Greedy attribution prune.
                print(f"certify MIXED → prune round {prune_rounds_done + 1} "
                      f"(orders={measured})")
                ablate_doc = ablate_fn(spec, out_dir, measured)
                new_orders, records = greedy_prune_orders(
                    spec, out_dir, measured, terminal_doc, ablate_doc,
                    round_n=prune_rounds_done + 1,
                )
                for rec in records:
                    print(f"  drop order={rec['dropped_order']} "
                          f"fn={rec['fn']!r} row={rec['violated_row']} "
                          f"Δ={rec['delta_pct']:+.4f}% "
                          f"evidence={rec['evidence']}")
                prune_rounds_done += 1
                if not new_orders:
                    msg = (
                        "STOP: prune emptied the measured set — "
                        "no shippable sub-bundle under policy"
                    )
                    print(f"certify {msg}", file=sys.stderr)
                    return CertifyResult(
                        exit_code=EXIT_WORK_ORDER,
                        verdict=TERMINAL_MIXED,
                        measured_orders=[],
                        message=msg,
                        prune_rounds=prune_rounds_done,
                        terminal_path=Path(terminal_path) if terminal_path else None,
                    )
                measured = new_orders
                # Worktrees for prior shipping set are stale after drops.
                if owned_worktrees and target is not None:
                    for d in (candidate_dir, baseline_dir):
                        if d is not None:
                            try:
                                target.remove_worktree(d)
                            except Exception:
                                pass
                    baseline_dir = candidate_dir = None
                    owned_worktrees = False
                    target = None

                round_n += 1
                tr = terminal_fn(
                    spec, out_dir, measured, round_n,
                    baseline_dir=None, candidate_dir=None, target=None,
                )
                terminal_path = Path(tr["path"])
                terminal_doc = tr["doc"]
                print(f"certify terminal-c{round_n}: {tr['verdict']} "
                      f"→ {terminal_path}")
                continue

            # REGRESSED / UNTOUCHED / TEST_FAILED / anything else
            nxt = _decision_next(verdict)
            msg = f"STOP: {verdict} — {nxt}"
            print(f"certify {msg}", file=sys.stderr)
            return CertifyResult(
                exit_code=EXIT_WORK_ORDER,
                verdict=verdict,
                measured_orders=list(measured),
                message=msg,
                prune_rounds=prune_rounds_done,
                terminal_path=Path(terminal_path) if terminal_path else None,
            )

    except CertifyStop as e:
        print(f"certify STOP (work order): {e.message}", file=sys.stderr)
        return CertifyResult(
            exit_code=EXIT_WORK_ORDER,
            verdict=e.verdict,
            measured_orders=list(measured),
            message=e.message,
            prune_rounds=prune_rounds_done,
            terminal_path=Path(terminal_path) if terminal_path else None,
            violations=list(e.violations or []),
        )
    except CertifyError as e:
        print(f"certify ERROR: {e}", file=sys.stderr)
        return CertifyResult(exit_code=EXIT_ERROR, message=str(e))
    except Exception as e:
        print(f"certify ERROR: {e}", file=sys.stderr)
        return CertifyResult(exit_code=EXIT_ERROR, message=str(e))
    finally:
        if owned_worktrees and target is not None:
            for d in (candidate_dir, baseline_dir):
                if d is not None:
                    try:
                        target.remove_worktree(d)
                    except Exception:
                        pass


def cli(args) -> None:
    """``aro certify <spec> --manifest DIR [--orders] [--from STAGE]``."""
    from . import spec as specmod

    sp = specmod.load(args.spec)
    out_dir = Path(args.manifest)
    if out_dir.is_file():
        out_dir = out_dir.parent
    if not out_dir.is_dir():
        raise SystemExit(f"aro certify: --manifest not a directory: {out_dir}")

    from_stage = getattr(args, "from_stage", None) or "recheck"
    orders = getattr(args, "orders", None)

    result = certify(
        sp, out_dir,
        orders=orders,
        from_stage=from_stage,
    )
    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    from .cli import main as _cli_main
    _cli_main(["certify"] + sys.argv[1:])
