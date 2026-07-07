"""`aro next` — the next-action oracle: one command that reads everything the
system has recorded and prints THE next action, why, and the exact command.

This is the automation seam. Every lifecycle capability is an explicit command
(sweep / attempt / coverage / recheck / union / clean / harvest), which means
the SEQUENCING otherwise lives in a human's head. The oracle moves the
sequencing into a computation over recorded state:

    ledger (permtree) · campaign state file · manifest · recheck · coverage
    artifact · merge-gate conflicts  →  one action

Deterministic and read-only (`--mark harvested` is the one deliberate
exception: harvest completion is real state the disk cannot infer, so the
operator records it here). Judgment stays OUTSIDE: the oracle names the action;
the operator (an agent following skill/references/campaign-operator.md) applies
the judgment the action needs (L1 health, PR content, conflict calls) and the
human keeps only ignition budget, upstream merges, and escalations.

Priority ladder (first match wins) — the WHY of the order:
  re-pin      a baseline that doesn't resolve/isn't an ancestor poisons every
              other signal — fix trust first
  manifest    a finished run without its hand-off artifact blocks harvest
  harvest     judged wins sitting unharvested are value on the table
  re-run      region churn: the judged code no longer exists as judged
  debts       open noise-limited / never-tried nodes; pending-first pays them
  factory     author-error left coverage boundary 3 dishonestly open
  coverage    an exhaustion claim with no dark-region footnote (or a footnote
              that predates the last campaign)
  dark        named dark regions exist: author a workload that lights one
  watch       everything closed — periodic recheck is all that remains

Anti-loop rules (what keeps the ladder from cycling forever):
  - a debt set UNCHANGED by the campaign that tried to pay it stops driving
    pay-debts: it is the probe-capped measurement floor (warned, not re-fueled);
  - a coverage report older than the last campaign is stale: re-measure before
    acting on its dark list (so light-dark always works from fresh facts);
  - dark fns surviving two consecutive light-dark campaigns are judged
    harness-unreachable by the OPERATOR (campaign-operator.md) — that one is a
    judgment, not a computation, because "the workload cannot reach it" and
    "the author has not found it yet" look identical on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import permtree


def gather(spec, spec_path: str = "") -> dict:
    """Read every recorded signal (best-effort; a missing piece is a fact, not
    an error). Pure IO — all decisions live in decide(). `spec_path` is the
    file path commands must name (the name alone is not runnable)."""
    from . import coverage as covmod
    from . import recheck as rcmod
    rows = permtree.load(spec.name)
    st = permtree.load_state(spec.name) or {}
    manifest = None
    out_dir = st.get("out_dir")
    if out_dir and (Path(out_dir) / "manifest.json").exists():
        try:
            m = json.loads((Path(out_dir) / "manifest.json").read_text())
            acc = m.get("accepted") or []
            manifest = {"accepted": len(acc),
                        "mergeable": sum(1 for a in acc if a.get("mergeable"))}
        except Exception:
            manifest = None
    try:
        rc = rcmod.assess(spec)
    except Exception as e:
        rc = {"verdict": "unknown", "reason": str(e)[:160]}
    dark = None
    stale = False
    gp = covmod.gap_path(spec.name)
    if gp.exists():
        try:
            dark = len(json.loads(gp.read_text()).get("dark_fns") or [])
        except Exception:
            dark = None
        # a report older than the last campaign predates its new workloads /
        # baseline: measuring again must come before acting on its dark list
        sp_ = permtree.state_path(spec.name)
        if sp_.exists() and gp.stat().st_mtime < sp_.stat().st_mtime:
            stale = True
    conflicts = permtree.union().get("conflicts", [])
    return {"spec": spec_path or spec.name, "has_ledger": bool(rows),
            "debts": permtree.open_debts(rows),
            "debt_keys": permtree.debt_keys(rows),
            "campaign_state": st, "manifest": manifest, "recheck": rc,
            "coverage_dark": dark, "coverage_stale": stale,
            "conflicts": conflicts}


def decide(s: dict) -> dict:
    """The priority ladder. Returns {action, command, why, warnings} — exactly
    one action; conflicts always ride along as warnings so no later step ships
    a contradicted number silently."""
    spec = s.get("spec", "<spec>")
    warnings = [f"merge-gate conflict: {c['fn']} ("
                + ", ".join(f"{wl}={v}" for wl, v in sorted(c["verdicts"].items()))
                + ") — resolve or disclose before any PR"
                for c in s.get("conflicts") or []]

    def act(action, command, why):
        return {"action": action, "command": command, "why": why,
                "warnings": warnings}

    st = s.get("campaign_state") or {}
    rc = s.get("recheck") or {}
    man = s.get("manifest")
    if rc.get("verdict") == "unknown":
        warnings.append("recheck unavailable ("
                        + (rc.get("reason") or "?")[:80]
                        + ") — the re-run/re-pin signals are blind here")

    if not s.get("has_ledger") and not st:
        return act("ignite-first",
                   f"python3 -m aro sweep {spec} ; judge the L1 map (operator), "
                   f"then: python3 -m aro sweep {spec} --attempt --diverge "
                   f"--critic --workloads 3",
                   "no campaign has ever run against this spec — map the "
                   "frontier, judge its health, then ignite")
    if rc.get("verdict") == "re-pin":
        return act("re-pin",
                   f"python3 -m aro recheck {spec}",
                   "the pinned baseline cannot be trusted "
                   f"({rc.get('reason', '')}) — every other signal is downstream "
                   "of this; escalate to the human with the recheck output")
    if st.get("out_dir") and man is None:
        return act("rebuild-manifest",
                   f"python3 -m aro manifest {st['out_dir']}",
                   "the last run has no readable manifest — the hand-off "
                   "artifact must exist before any harvest")
    if man and man.get("accepted") and not st.get("harvested"):
        return act("harvest",
                   f"evaluate the run per skill/references/evaluate-run.md "
                   f"(manifest: {st.get('out_dir')}/manifest.json, "
                   f"{man['mergeable']}/{man['accepted']} mergeable); then: "
                   f"python3 -m aro next {spec} --mark harvested",
                   "judged wins are sitting unharvested — decide first, act "
                   "second, honor the merge gate; mark harvested when decided")
    if rc.get("verdict") == "re-run":
        return act("re-run",
                   f"python3 -m aro recheck {spec}   # prints the re-pin / "
                   f"re-derive-DIFF / L1-first procedure",
                   f"the target moved under the editable regions "
                   f"({len(rc.get('region_churn') or [])} file(s)) — the judged "
                   "code no longer exists as judged")
    if s.get("debts"):
        fns = sorted({d.get("fn") for d in s["debts"]})
        # anti-loop: the SAME debt set the last campaign already left behind is
        # not payable by running again — it is the probe-capped measurement
        # floor (off-profile residue / noise the factory could not rescue).
        # Report it and let the ladder continue instead of re-igniting forever.
        if s.get("debt_keys") == st.get("debts_open"):
            warnings.append(f"{len(s['debts'])} open node(s) unchanged by the "
                            f"last campaign ({', '.join(fns[:6])}"
                            f"{' …' if len(fns) > 6 else ''}) — treat as the "
                            "probe-capped measurement floor, not as re-ignition "
                            "fuel")
        else:
            return act("pay-debts",
                       f"python3 -m aro sweep {spec} --attempt --diverge --critic "
                       f"--probe-factory --workloads 3",
                       f"{len(s['debts'])} open node(s) in the ledger "
                       f"({', '.join(fns[:6])}{' …' if len(fns) > 6 else ''}) — "
                       "pending-first re-attempts them ahead of fresh frontier; "
                       "the probe factory is the resolver for noise-limited debt")
    if str(st.get("state", "")).startswith("author-error"):
        return act("retry-factory",
                   f"python3 -m aro sweep {spec} --attempt --diverge --critic "
                   f"--workloads 3",
                   f"the last campaign closed {st.get('state')} — an "
                   "infrastructure failure, not a dry factory; coverage "
                   "boundary 3 is still open")
    if s.get("coverage_dark") is None or s.get("coverage_stale"):
        return act("coverage",
                   f"python3 -m aro coverage {spec}",
                   "no dark-region report exists — an exhaustion claim without "
                   "its coverage footnote is not honest yet"
                   if s.get("coverage_dark") is None else
                   "the dark-region report predates the last campaign — "
                   "re-measure before acting on its dark list")
    if s.get("coverage_dark"):
        return act("light-dark-regions",
                   f"python3 -m aro sweep {spec} --attempt --diverge --critic "
                   f"--workloads 3   # the factory's author prompt now carries "
                   f"the named dark targets",
                   f"{s['coverage_dark']} function(s) no registered workload "
                   "executes — a new workload that lights a named dark region "
                   "beats a distribution shift")
    return act("watch",
               f"python3 -m aro recheck {spec}   # on a cadence (e.g. after "
               f"upstream merges)",
               "ledger clear, factory closed, coverage lit — exhaustion holds "
               "until the target repo moves")


def cli(args) -> None:
    from . import spec as specmod
    sp = specmod.load(args.spec)
    if args.mark:
        if args.mark != "harvested":
            raise SystemExit(f"unknown mark {args.mark!r} (only: harvested)")
        st = permtree.mark_state(sp.name, harvested=True)
        print(f"marked harvested in {permtree.state_path(sp.name)} "
              f"(state: {st.get('state', '?')})")
        return
    s = gather(sp, spec_path=args.spec)
    d = decide(s)
    if args.json:
        inputs = {k: v for k, v in s.items() if k != "debts"}
        inputs["debts"] = len(s["debts"])
        print(json.dumps({**d, "inputs": inputs}, ensure_ascii=False, indent=1))
        return
    st = s.get("campaign_state") or {}
    bits = []
    if st:
        bits.append(f"last campaign: {st.get('state', '?')}"
                    + (" · harvested" if st.get("harvested") else ""))
    if s["manifest"]:
        bits.append(f"manifest {s['manifest']['mergeable']}/"
                    f"{s['manifest']['accepted']} mergeable")
    bits.append(f"{len(s['debts'])} open debt(s)")
    if s.get("coverage_dark") is not None:
        bits.append(f"{s['coverage_dark']} dark fn(s)")
    bits.append(f"recheck: {(s.get('recheck') or {}).get('verdict', '?')}")
    print(f"state: {' · '.join(bits)}")
    print(f"next : {d['command']}")
    print(f"why  : {d['why']}")
    for w in d["warnings"]:
        print(f"warn : {w}")
