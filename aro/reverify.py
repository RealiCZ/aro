"""`aro reverify` — re-adjudicate frozen manifest candidates through current gates.

After a gate-hardening deploy (new differential probe, full-suite `test_full`
tier, …) the operator must mechanically re-check every accepted entry in an
existing campaign's `manifest.json` — no human diff archaeology. This module
replays the patches in manifest order on a single compounding worktree, runs
the CURRENT correctness chain (build → test → optional test_full →
differential vs a pristine baseline), and reports which entries survive.

Before any candidate is gated, a pre-flight runs build → test on the
*unpatched* baseline worktree. If that fails, the environment is broken
(PATH/toolchain/etc.): the run aborts as `preflight: "fail"` with empty
entries and attributes nothing to candidates.

Replay semantics matter: campaign accepts advanced the baseline, so later
SEARCH blocks may only match after earlier patches. Failures are reverted so
subsequent entries still see the last good state; unappliable entries leave the
tree untouched (snapshot restore). When manifest entries carry the explicit
acceptance chain (`acceptance_seq` + `parent`), reverify validates that chain
before any worktree work and replays in the proven order; old manifests without
those fields keep order-based replay with a one-line legacy notice.

`--apply` stamps each entry additively (`"reverify": {verdict, failing_gate?}`)
and forces `mergeable=false` on every non-`reverify-pass`. It NEVER sets
`mergeable=true` — promotion stays a human decision. Pre-flight failure
never mutates the manifest even with `--apply`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, Optional

from . import patchfile
from .eval import _gate_detail_tail, run_correctness_gates
from .manifest import validate_acceptance_chain
from .types import Edit, Patch

VERDICT_PASS = "reverify-pass"
VERDICT_FAIL = "reverify-fail"
VERDICT_UNAPPLIABLE = "unappliable"
VERDICT_SKIPPED = "skipped"

PREFLIGHT_FAIL_MSG = (
    "pre-flight failed: the UNPATCHED baseline does not build/test — "
    "fix the environment (e.g. PATH/toolchain), no candidate was judged"
)

LEGACY_CHAIN_NOTICE = (
    "manifest has no acceptance chain — replaying by order (legacy)"
)


def _entries_have_acceptance_chain(entries) -> bool:
    """True when any entry carries acceptance_seq or parent (new manifests)."""
    for e in entries or []:
        if "acceptance_seq" in e or "parent" in e:
            return True
    return False


def reverse_patch(patch: Patch) -> Patch:
    """Inverse SEARCH/REPLACE (edits reversed so multi-edit undos compose)."""
    return Patch(edits=[
        Edit(e.path, e.replace, e.search) for e in reversed(patch.edits)
    ])


def parse_orders(s: Optional[str]) -> Optional[set]:
    """Parse `--orders 1,3,8` into a set of 1-based ints, or None (= all)."""
    if s is None or str(s).strip() == "":
        return None
    out = set()
    for tok in str(s).split(","):
        tok = tok.strip()
        if not tok:
            continue
        out.add(int(tok))
    return out or None


def _snapshot_paths(work, paths) -> dict:
    snaps = {}
    for p in paths:
        f = Path(work) / p
        snaps[p] = f.read_text() if f.exists() else None
    return snaps


def _restore_snapshot(work, snaps: dict) -> None:
    for p, content in snaps.items():
        f = Path(work) / p
        if content is None:
            if f.exists():
                f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)


def load_entry_patch(out_dir: Path, entry: dict) -> Patch:
    """Load the SEARCH/REPLACE patch for one manifest accepted entry."""
    rel = entry.get("patch_path")
    if rel:
        pf = Path(out_dir) / rel
    else:
        attempt = entry.get("attempt")  # "a1" or None
        cid = entry.get("id") or ""
        base = (Path(out_dir) / attempt) if attempt else Path(out_dir)
        pf = base / "patches" / (patchfile.safe_id(cid) + ".txt")
    if not pf.exists():
        raise FileNotFoundError(f"patch not found for entry order="
                                f"{entry.get('order')}: {pf}")
    return Patch(edits=patchfile.parse(pf.read_text()))


def _gate_config_summary(spec, test_full_cmd) -> dict:
    summary = {
        "build": list(getattr(spec, "build", None) or []),
        "test": list(getattr(spec, "test", None) or []),
        "differential": bool(getattr(spec, "differential", None)),
    }
    if test_full_cmd is not None:
        summary["test_full"] = list(test_full_cmd)
    return summary


def _probe_name(spec) -> Optional[str]:
    d = getattr(spec, "differential", None) or {}
    return d.get("example") if isinstance(d, dict) else None


def _preflight_baseline(target, baseline_work) -> dict:
    """Build → fast test on the pristine baseline (no test_full, no differential).

    Same build/test helpers and detail-tail as the replay gate chain. Returns
    `{ok, detail, n_pass}` — on success `n_pass` is the baseline pass count for
    the regression gate (reuses this test; no second baseline worktree).
    """
    try:
        target.build(baseline_work)
    except Exception as e:
        return {"ok": False, "detail": _gate_detail_tail(f"build failed: {e}"),
                "n_pass": None}
    try:
        n_pass = target.test(baseline_work)
    except Exception as e:
        return {"ok": False, "detail": _gate_detail_tail(f"tests failed: {e}"),
                "n_pass": None}
    return {"ok": True, "detail": "", "n_pass": n_pass}


def reverify(spec, out_dir, *, orders=None, apply: bool = False,
             target=None, test_full_runner: Optional[Callable] = None,
             n_pre=None) -> dict:
    """Replay manifest accepted patches through the current correctness gates.

    Parameters
    ----------
    spec : TargetSpec
    out_dir : path to the campaign run dir (must contain manifest.json + patches)
    orders : optional set of 1-based order ints to GATE; others still apply
             (compounding) but are marked `skipped`. None = gate all.
    apply : when True, stamp reverify onto each manifest entry and force
            mergeable=false on non-pass (never promotes mergeable).
            Ignored entirely when pre-flight fails.
    target : injectable SpecTarget-like object (tests); production builds one.
    test_full_runner : injectable (stdout, stderr, rc) runner for hermetic tests.
    n_pre : optional baseline pass count; when None, taken from the pre-flight
            test on the pristine baseline worktree.

    Returns the reverify.json document (also written under out_dir). On
    pre-flight failure the document has `preflight: "fail"`, a `detail` tail,
    and an empty `entries` list — no candidate is judged.
    """
    out_dir = Path(out_dir)
    man_path = out_dir / "manifest.json"
    if not man_path.exists():
        raise FileNotFoundError(f"no manifest.json in {out_dir}")
    manifest = json.loads(man_path.read_text())
    raw_accepted = list(manifest.get("accepted") or [])
    # Fail-fast: chain inconsistencies abort before any worktree work (like preflight).
    validate_acceptance_chain(raw_accepted)
    has_chain = _entries_have_acceptance_chain(raw_accepted)
    if not has_chain:
        print(LEGACY_CHAIN_NOTICE)
    # Chain fields present ⇒ validator proved order == compounding chronology;
    # either way we replay sorted by order (legacy path when fields absent).
    entries = sorted(raw_accepted, key=lambda e: e.get("order") or 0)
    order_filter = orders if isinstance(orders, (set, frozenset)) else parse_orders(orders)

    from .terminal import resolve_test_full, resolve_test_full_timeout
    test_full_cmd = resolve_test_full(spec)
    test_full_timeout = (resolve_test_full_timeout(spec)
                         if test_full_cmd is not None else None)

    if target is None:
        from .target import SpecTarget
        target = SpecTarget(spec)

    baseline_work = target.make_worktree("reverify-base")
    work = None
    results = []
    preflight = "pass"
    preflight_detail = ""
    try:
        # Environment gate: unpatched baseline must build+test before any
        # candidate is attributed. Reuses this worktree for differential / n_pre.
        pf = _preflight_baseline(target, baseline_work)
        if not pf["ok"]:
            preflight = "fail"
            preflight_detail = pf["detail"] or ""
        else:
            if n_pre is None:
                n_pre = pf["n_pass"]
            work = target.make_worktree("reverify-replay")

            for entry in entries:
                order = entry.get("order")
                cid = entry.get("id")
                fn = entry.get("fn")
                row = {"order": order, "id": cid, "fn": fn,
                       "verdict": VERDICT_UNAPPLIABLE, "gates": {}, "detail": ""}
                try:
                    patch = load_entry_patch(out_dir, entry)
                except Exception as e:
                    row["verdict"] = VERDICT_UNAPPLIABLE
                    row["detail"] = f"load patch: {e}"
                    results.append(row)
                    continue

                paths = [e.path for e in patch.edits]
                snaps = _snapshot_paths(work, paths)
                try:
                    target.apply(patch, work)
                except Exception as e:
                    _restore_snapshot(work, snaps)
                    row["verdict"] = VERDICT_UNAPPLIABLE
                    row["detail"] = f"apply failed: {e}"
                    results.append(row)
                    continue

                # Skipped orders: keep the applied patch (compounding), no gates.
                if order_filter is not None and order not in order_filter:
                    row["verdict"] = VERDICT_SKIPPED
                    row["detail"] = "skipped by --orders (applied for compounding)"
                    results.append(row)
                    continue

                gate = run_correctness_gates(
                    target, work, baseline_work, n_pre=n_pre,
                    test_full_cmd=test_full_cmd,
                    test_full_timeout=test_full_timeout,
                    test_full_runner=test_full_runner)
                row["gates"] = dict(gate.get("gates") or {})
                if gate["ok"]:
                    row["verdict"] = VERDICT_PASS
                    row["detail"] = ""
                    results.append(row)
                    continue

                # Gate failed: revert this patch so later entries see last good state.
                try:
                    target.apply(reverse_patch(patch), work)
                except Exception:
                    # Fall back to the pre-apply snapshot if reverse apply fails.
                    _restore_snapshot(work, snaps)
                row["verdict"] = VERDICT_FAIL
                row["detail"] = gate.get("detail") or ""
                row["failing_gate"] = gate.get("failing_gate")
                results.append(row)
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

    doc = {
        "spec": getattr(spec, "name", None) or manifest.get("spec"),
        "baseline_ref": (getattr(spec, "baseline_ref", None)
                         or manifest.get("baseline_ref")),
        "gate_config_summary": _gate_config_summary(spec, test_full_cmd),
        "probe": _probe_name(spec),
        "preflight": preflight,
        "entries": results,
    }
    if preflight == "fail":
        doc["detail"] = preflight_detail

    out_path = out_dir / "reverify.json"
    out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")

    # Pre-flight failure: never stamp the manifest, even with --apply.
    if apply and preflight == "pass":
        _stamp_manifest(manifest, results, man_path)

    return doc


def _stamp_manifest(manifest: dict, results: list, man_path: Path) -> None:
    """Stamp reverify onto accepted entries. Never promotes mergeable=true."""
    by_order = {r["order"]: r for r in results}
    for entry in manifest.get("accepted") or []:
        r = by_order.get(entry.get("order"))
        if r is None:
            continue
        stamp = {"verdict": r["verdict"]}
        if r.get("failing_gate"):
            stamp["failing_gate"] = r["failing_gate"]
        entry["reverify"] = stamp
        if r["verdict"] != VERDICT_PASS:
            entry["mergeable"] = False
    man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")


def _print_table(doc: dict) -> None:
    print(f"reverify {doc.get('spec')} @ {doc.get('baseline_ref')}  "
          f"probe={doc.get('probe') or '(none)'}")
    print(f"{'order':>5}  {'id':<24}  {'fn':<20}  {'verdict':<16}  failing_gate")
    for r in doc.get("entries") or []:
        fg = r.get("failing_gate") or ""
        print(f"{r.get('order') or '?':>5}  {str(r.get('id') or ''):<24}  "
              f"{str(r.get('fn') or ''):<20}  {r.get('verdict'):<16}  {fg}")
    n = len(doc.get("entries") or [])
    n_pass = sum(1 for r in (doc.get("entries") or [])
                 if r.get("verdict") == VERDICT_PASS)
    n_fail = sum(1 for r in (doc.get("entries") or [])
                 if r.get("verdict") == VERDICT_FAIL)
    n_un = sum(1 for r in (doc.get("entries") or [])
               if r.get("verdict") == VERDICT_UNAPPLIABLE)
    n_sk = sum(1 for r in (doc.get("entries") or [])
               if r.get("verdict") == VERDICT_SKIPPED)
    print(f"  {n} entr{'y' if n == 1 else 'ies'}: {n_pass} pass · "
          f"{n_fail} fail · {n_un} unappliable · {n_sk} skipped")


def cli(args) -> None:
    from . import spec as specmod

    sp = specmod.load(args.spec)
    orders = parse_orders(getattr(args, "orders", None))
    doc = reverify(
        sp, args.out,
        orders=orders,
        apply=bool(getattr(args, "apply", False)),
    )
    out_path = Path(args.out) / "reverify.json"
    if doc.get("preflight") == "fail":
        print(PREFLIGHT_FAIL_MSG, file=sys.stderr)
        if doc.get("detail"):
            print(doc["detail"], file=sys.stderr)
        print(f"reverify.json → {out_path}")
        raise SystemExit(1)
    _print_table(doc)
    print(f"reverify.json → {out_path}")
    if getattr(args, "apply", False):
        print("manifest stamped (mergeable forced false on non-pass; "
              "never auto-promoted)")


if __name__ == "__main__":
    from .cli import main as _cli_main
    _cli_main(["reverify"] + sys.argv[1:])
