"""`aro ablate` — per-entry terminal attribution + greedy shippable sub-bundle.

When a multi-candidate bundle lands TERMINAL_MIXED, the operator must attribute
which entries caused protected regressions vs tradeable ones. This module walks
the acceptance chain, measures marginal terminal deltas after each applied
entry, judges each marginal against the row-family policy (hysteresis + one-shot
resolution upgrade for band rows), and proposes the largest shippable survivor
list in chain order.

**Proposal tool only.** Ablate never mutates the manifest and never stamps
anything. Certification of the proposed sub-bundle remains `aro terminal` on a
worktree with the survivors applied.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

from .icount import ir_epsilon_pct
from .manifest import validate_acceptance_chain
from .reverify import (
    LEGACY_CHAIN_NOTICE,
    PREFLIGHT_FAIL_MSG,
    _entries_have_acceptance_chain,
    _preflight_baseline,
    _restore_snapshot,
    _snapshot_paths,
    load_entry_patch,
    parse_orders,
    reverse_patch,
)
from .terminal import (
    TerminalError,
    classify_subject_regression,
    has_terminal_config,
    judge_terminal,
    load_floors,
    measure_checkout_rounds,
    package_name,
    resolve_control_composition_bound_pct,
    resolve_control_lanes,
    resolve_default_floor_pct,
    resolve_measure_bin,
    resolve_protected_hysteresis,
    resolve_protected_row_families,
    resolve_terminal_rounds,
    resolve_terminal_timeout,
    resolve_tradeable_regression_cap_pct,
    terminal_bench_filter,
    terminal_bench_targets,
)

POLICY_KEEP = "keep"
POLICY_DROP = "drop"
POLICY_BAND = "band"
VERDICT_UNAPPLIABLE_AFTER_DROP = "unappliable-after-drop"

DEFAULT_UPGRADE_ROUNDS = 5


def entry_policy_verdict(result, *, protected_families, cap_pct,
                        hysteresis) -> str:
    """Map a marginal TerminalResult to keep / drop / band under row-family policy.

    Control anomalies and protected violations (past hysteresis) or tradeable
    regressions beyond cap → drop. Protected regressions inside the hysteresis
    band → band (caller may resolution-upgrade). Otherwise keep.
    """
    from .terminal import TERMINAL_CONTROL_ANOMALY

    if result.verdict == TERMINAL_CONTROL_ANOMALY:
        return POLICY_DROP
    families = [str(x) for x in (protected_families or [])]
    if not families:
        # No policy: any subject regression blocks shipping the entry alone.
        if any(str(getattr(r, "status", "") or "") == "regressed"
               for r in (result.rows or [])):
            return POLICY_DROP
        return POLICY_KEEP

    cap = float(cap_pct) if cap_pct is not None else 0.0
    saw_band = False
    for r in result.rows or []:
        st = str(getattr(r, "status", "") or "")
        if st != "regressed":
            continue
        kind = classify_subject_regression(
            r, protected_families=families, cap_pct=cap,
            hysteresis=hysteresis)
        if kind == "violation":
            return POLICY_DROP
        if kind == "band":
            saw_band = True
    if saw_band:
        return POLICY_BAND
    return POLICY_KEEP


def _marginal_summary(result) -> dict:
    """Nonzero Δ map for ablate.json (capped already by judge)."""
    return dict(result.bench_ir_rows or {})


def _measure_default(checkout, *, package, bench_targets, measure_bin,
                     rounds, bench_filter, timeout, runner):
    return measure_checkout_rounds(
        checkout, package=package, bench_targets=bench_targets,
        measure_bin=measure_bin, rounds=rounds, bench_filter=bench_filter,
        timeout=timeout, runner=runner)


def ablate(spec, out_dir, *, orders=None, rounds=None,
           upgrade_rounds: int = DEFAULT_UPGRADE_ROUNDS,
           dry_run: bool = False, target=None,
           measure_fn: Optional[Callable] = None,
           runner: Optional[Callable] = None,
           floors: Optional[dict] = None) -> dict:
    """Attribute terminal marginals along the acceptance chain; propose survivors.

    Parameters
    ----------
    spec : TargetSpec
    out_dir : campaign run dir (manifest.json + patches)
    orders : optional set of 1-based orders to attribute; others still apply
             for compounding but are reported as skipped (no measure).
    rounds : measure rounds per prefix (default from spec / env)
    upgrade_rounds : one-shot re-measure rounds when a marginal lands in band
    dry_run : print the plan and return a header-only doc (no measure)
    target : injectable SpecTarget-like (tests); production builds one
    measure_fn : injectable (checkout, *, rounds) -> MeasureDoc (tests)
    runner : injectable measure subprocess runner (production path)
    floors : optional floor map override (tests)

    Returns the ablate.json document (also written under out_dir). Preflight
    failure → empty entries, zero attribution. Never mutates the manifest.
    """
    out_dir = Path(out_dir)
    man_path = out_dir / "manifest.json"
    if not man_path.exists():
        raise FileNotFoundError(f"no manifest.json in {out_dir}")
    if not has_terminal_config(spec):
        raise TerminalError(
            "spec has no terminal_bench_targets — ablate requires terminal config")

    manifest = json.loads(man_path.read_text())
    raw_accepted = list(manifest.get("accepted") or [])
    validate_acceptance_chain(raw_accepted)
    has_chain = _entries_have_acceptance_chain(raw_accepted)
    if not has_chain:
        print(LEGACY_CHAIN_NOTICE)
    entries = sorted(raw_accepted, key=lambda e: e.get("order") or 0)
    order_filter = (orders if isinstance(orders, (set, frozenset))
                    else parse_orders(orders))

    n_rounds = int(rounds) if rounds is not None else resolve_terminal_rounds(spec)
    n_upgrade = int(upgrade_rounds)
    if n_upgrade < 1:
        raise TerminalError("upgrade_rounds must be >= 1")

    families = resolve_protected_row_families(spec)
    cap = resolve_tradeable_regression_cap_pct(spec) if families else None
    hyst = resolve_protected_hysteresis(spec) if families else None
    lanes = resolve_control_lanes(spec)
    bound = (resolve_control_composition_bound_pct(spec) if lanes else None)
    eps = ir_epsilon_pct(spec)
    default_fl = resolve_default_floor_pct(spec)
    name = getattr(spec, "name", None) or "unknown"

    if floors is not None:
        floor_map = dict(floors)
    else:
        floor_map, _meta, _fw = load_floors(str(name))

    policy_header = {
        "protected_row_families": list(families),
        "tradeable_regression_cap_pct": cap,
        "protected_hysteresis": hyst,
    }

    doc_header = {
        "spec": getattr(spec, "name", None) or manifest.get("spec"),
        "baseline_ref": (getattr(spec, "baseline_ref", None)
                         or manifest.get("baseline_ref")),
        "preflight": "pass",
        "rounds": n_rounds,
        "upgrade_rounds": n_upgrade,
        "policy": policy_header,
        "entries": [],
        "proposal": [],
        "dropped": [],
        "unappliable_after_drop": [],
    }

    if dry_run:
        plan = []
        for e in entries:
            o = e.get("order")
            gated = order_filter is None or o in order_filter
            plan.append({
                "order": o, "id": e.get("id"), "fn": e.get("fn"),
                "action": "measure" if gated else "apply-only",
            })
        doc_header["dry_run"] = True
        doc_header["plan"] = plan
        print(f"ablate dry-run for {doc_header['spec']} @ {doc_header['baseline_ref']}")
        print(f"  rounds={n_rounds} upgrade_rounds={n_upgrade}")
        print(f"  policy families={families or '(none)'}")
        print(f"  entries: {len(plan)} "
              f"({sum(1 for p in plan if p['action'] == 'measure')} measured)")
        for p in plan:
            print(f"    order={p['order']} {p['id']} → {p['action']}")
        print("  (proposal tool only — never stamps the manifest; "
              "certify survivors with `aro terminal`)")
        out_path = out_dir / "ablate.json"
        out_path.write_text(json.dumps(doc_header, ensure_ascii=False, indent=1)
                            + "\n")
        return doc_header

    if target is None:
        from .target import SpecTarget
        target = SpecTarget(spec)

    targets = terminal_bench_targets(spec)
    filt = terminal_bench_filter(spec)
    pkg = package_name(spec)
    try:
        bin_path = resolve_measure_bin(spec)
    except TerminalError:
        bin_path = None
    to = resolve_terminal_timeout(spec)

    def _do_measure(checkout, *, n):
        if measure_fn is not None:
            return measure_fn(checkout, rounds=n)
        if bin_path is None:
            raise TerminalError(
                "measure binary unset: set ARO_MEASURE_BIN or measure_bin")
        return _measure_default(
            checkout, package=pkg, bench_targets=targets,
            measure_bin=bin_path, rounds=n, bench_filter=filt,
            timeout=to, runner=runner)

    baseline_work = target.make_worktree("ablate-base")
    work = None
    results = []
    preflight = "pass"
    preflight_detail = ""
    try:
        pf = _preflight_baseline(target, baseline_work)
        if not pf["ok"]:
            preflight = "fail"
            preflight_detail = pf["detail"] or ""
        else:
            work = target.make_worktree("ablate-replay")
            prev_doc = _do_measure(work, n=n_rounds)

            for entry in entries:
                order = entry.get("order")
                cid = entry.get("id")
                fn = entry.get("fn")
                row = {
                    "order": order, "id": cid, "fn": fn,
                    "marginal_rows_summary": {},
                    "policy_verdict": POLICY_KEEP,
                    "upgraded": False,
                }
                try:
                    patch = load_entry_patch(out_dir, entry)
                except Exception as e:
                    row["policy_verdict"] = "unappliable"
                    row["detail"] = f"load patch: {e}"
                    results.append(row)
                    continue

                paths = [e.path for e in patch.edits]
                snaps = _snapshot_paths(work, paths)
                try:
                    target.apply(patch, work)
                except Exception as e:
                    _restore_snapshot(work, snaps)
                    row["policy_verdict"] = "unappliable"
                    row["detail"] = f"apply failed: {e}"
                    results.append(row)
                    continue

                if order_filter is not None and order not in order_filter:
                    row["policy_verdict"] = "skipped"
                    row["detail"] = "skipped by --orders (applied for compounding)"
                    # Keep prev_doc — skipped entries still advance the tree but
                    # their marginal is not attributed; next measure is vs the
                    # post-skip state (same as measuring through).
                    prev_doc = _do_measure(work, n=n_rounds)
                    results.append(row)
                    continue

                curr_doc = _do_measure(work, n=n_rounds)
                result = judge_terminal(
                    prev_doc, curr_doc,
                    epsilon_pct=eps,
                    floors=floor_map,
                    default_floor_pct=default_fl,
                    floors_source="ablate",
                    rounds=n_rounds,
                    control_lanes=lanes or None,
                    control_composition_bound_pct=bound,
                    protected_row_families=families or None,
                    tradeable_regression_cap_pct=cap,
                    protected_hysteresis=hyst,
                )
                pv = entry_policy_verdict(
                    result, protected_families=families, cap_pct=cap,
                    hysteresis=hyst)
                upgraded = False

                if pv == POLICY_BAND:
                    # Resolution upgrade: re-measure JUST this prefix pair once.
                    try:
                        target.apply(reverse_patch(patch), work)
                        prev_up = _do_measure(work, n=n_upgrade)
                        target.apply(patch, work)
                        curr_up = _do_measure(work, n=n_upgrade)
                        result = judge_terminal(
                            prev_up, curr_up,
                            epsilon_pct=eps,
                            floors=floor_map,
                            default_floor_pct=default_fl,
                            floors_source="ablate",
                            rounds=n_upgrade,
                            control_lanes=lanes or None,
                            control_composition_bound_pct=bound,
                            protected_row_families=families or None,
                            tradeable_regression_cap_pct=cap,
                            protected_hysteresis=hyst,
                        )
                        pv = entry_policy_verdict(
                            result, protected_families=families, cap_pct=cap,
                            hysteresis=hyst)
                        # Upgraded median stands once — no retry loops.
                        upgraded = True
                        curr_doc = curr_up
                    except Exception:
                        # Fall back to the first measurement; keep band.
                        _restore_snapshot(work, snaps)
                        target.apply(patch, work)

                row["marginal_rows_summary"] = _marginal_summary(result)
                row["policy_verdict"] = pv
                row["upgraded"] = upgraded
                row["terminal_verdict"] = result.verdict
                results.append(row)
                # Compounding: always keep the applied entry for later SEARCH
                # context. Drop is a proposal-time filter, not a tree revert.
                prev_doc = curr_doc
    finally:
        if work is not None:
            try:
                target.remove_worktree(work)
            except Exception:
                pass
        try:
            target.remove_worktree(baseline_work)
        except Exception:
            pass

    doc = dict(doc_header)
    doc["preflight"] = preflight
    doc["entries"] = results
    if preflight == "fail":
        doc["detail"] = preflight_detail
        doc["entries"] = []
        doc["proposal"] = []
        doc["dropped"] = []
        doc["unappliable_after_drop"] = []
    else:
        dropped = [r for r in results if r.get("policy_verdict") == POLICY_DROP]
        survivors = [
            r for r in results
            if r.get("policy_verdict") in (POLICY_KEEP, POLICY_BAND)
        ]
        # Re-simulate apply of survivors only; mark SEARCH breakage honestly.
        unappliable = []
        proposal = []
        if target is not None:
            sim = None
            try:
                sim = target.make_worktree("ablate-propose")
                by_order = {e.get("order"): e for e in entries}
                for r in survivors:
                    ent = by_order.get(r.get("order"))
                    if ent is None:
                        continue
                    try:
                        patch = load_entry_patch(out_dir, ent)
                        target.apply(patch, sim)
                        proposal.append({
                            "order": r.get("order"),
                            "id": r.get("id"),
                            "fn": r.get("fn"),
                            "policy_verdict": r.get("policy_verdict"),
                        })
                    except Exception as e:
                        unappliable.append({
                            "order": r.get("order"),
                            "id": r.get("id"),
                            "fn": r.get("fn"),
                            "verdict": VERDICT_UNAPPLIABLE_AFTER_DROP,
                            "detail": str(e),
                        })
            finally:
                if sim is not None:
                    try:
                        target.remove_worktree(sim)
                    except Exception:
                        pass
        doc["proposal"] = proposal
        doc["dropped"] = [
            {"order": r.get("order"), "id": r.get("id"), "fn": r.get("fn"),
             "policy_verdict": POLICY_DROP,
             "marginal_rows_summary": r.get("marginal_rows_summary") or {}}
            for r in dropped
        ]
        doc["unappliable_after_drop"] = unappliable

    out_path = out_dir / "ablate.json"
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return doc


def _print_table(doc: dict) -> None:
    print(f"ablate {doc.get('spec')} @ {doc.get('baseline_ref')}  "
          f"rounds={doc.get('rounds')} upgrade={doc.get('upgrade_rounds')}")
    print(f"{'order':>5}  {'id':<24}  {'fn':<20}  {'policy':<8}  upg  terminal")
    for r in doc.get("entries") or []:
        print(f"{r.get('order') or '?':>5}  {str(r.get('id') or ''):<24}  "
              f"{str(r.get('fn') or ''):<20}  "
              f"{str(r.get('policy_verdict') or ''):<8}  "
              f"{'Y' if r.get('upgraded') else 'n':<3}  "
              f"{r.get('terminal_verdict') or ''}")
    prop = doc.get("proposal") or []
    dropped = doc.get("dropped") or []
    unapp = doc.get("unappliable_after_drop") or []
    print(f"  proposal: {len(prop)} survivor(s) · dropped={len(dropped)} · "
          f"unappliable-after-drop={len(unapp)}")
    if prop:
        print("  ship: " + ", ".join(
            f"{p.get('order')}:{p.get('id')}" for p in prop))
    print("  (proposal only — certify with `aro terminal`; never stamps manifest)")


def cli(args) -> None:
    from . import spec as specmod

    sp = specmod.load(args.spec)
    orders = parse_orders(getattr(args, "orders", None))
    rounds = getattr(args, "rounds", None)
    upgrade = getattr(args, "upgrade_rounds", None)
    if upgrade is None:
        upgrade = DEFAULT_UPGRADE_ROUNDS
    dry = bool(getattr(args, "dry_run", False))
    try:
        doc = ablate(
            sp, args.out,
            orders=orders,
            rounds=rounds,
            upgrade_rounds=int(upgrade),
            dry_run=dry,
        )
    except TerminalError as e:
        print(f"ablate ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)

    out_path = Path(args.out) / "ablate.json"
    if doc.get("preflight") == "fail":
        print(PREFLIGHT_FAIL_MSG, file=sys.stderr)
        if doc.get("detail"):
            print(doc["detail"], file=sys.stderr)
        print(f"ablate.json → {out_path}")
        raise SystemExit(1)
    if not dry:
        _print_table(doc)
    print(f"ablate.json → {out_path}")


if __name__ == "__main__":
    from .cli import main as _cli_main
    _cli_main(["ablate"] + sys.argv[1:])
