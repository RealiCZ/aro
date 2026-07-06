"""`aro recheck` — the computed re-run signal after the target repo moves.

A campaign's judgments (the permanent ledger, the exhaustion claim) are pinned
to the spec's `baseline_ref`. When the target repo advances — accepted PRs
merged, unrelated development — whether the campaign is stale is a computation,
not a feeling: diff the pinned baseline against the current head and check the
churn against the spec's EDITABLE regions.

  - churn under the editable regions → the judged code no longer exists as
    judged: RE-RUN suggested (re-pin the baseline, re-derive the differential
    DIFF hash, re-run L1 first);
  - head moved but the regions are untouched → the claim still stands
    (out-of-region churn — deps, other crates — is reported as a weak signal);
  - baseline not an ancestor of the head (rewrite / different branch) or
    unresolvable → re-pin before trusting anything.

Read-only: never fetches, never edits the spec.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from . import vcs
from .guard import _in_regions


def assess(spec, ref: str = "HEAD") -> dict:
    """Compare the spec's pinned baseline against `ref` in the target repo.
    Returns {verdict, reason, baseline, head, ahead, region_churn, other_churn}
    with verdict ∈ {current, still-current, re-run, re-pin}."""
    repo = Path(spec.repo)
    base = vcs.rev_parse(repo, spec.baseline_ref)
    head = vcs.rev_parse(repo, ref)
    if base is None or head is None:
        what = spec.baseline_ref if base is None else ref
        return {"verdict": "re-pin", "baseline": base, "head": head,
                "ahead": None, "region_churn": [], "other_churn": [],
                "reason": f"`{what}` does not resolve in {repo} — "
                          f"re-pin the spec's baseline_ref first"}
    if base == head:
        return {"verdict": "current", "baseline": base, "head": head,
                "ahead": 0, "region_churn": [], "other_churn": [],
                "reason": "the pinned baseline IS the head — nothing moved"}
    anc = vcs.git(repo, "merge-base", "--is-ancestor", base, head)
    if anc.returncode != 0:
        return {"verdict": "re-pin", "baseline": base, "head": head,
                "ahead": None, "region_churn": [], "other_churn": [],
                "reason": "the pinned baseline is not an ancestor of the head "
                          "(history rewrite or different branch) — re-pin first"}
    ahead = vcs.git(repo, "rev-list", "--count", f"{base}..{head}")
    n_ahead = int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else None
    diff = vcs.git(repo, "diff", "--name-only", base, head)
    changed = [f for f in (diff.stdout or "").splitlines() if f.strip()]
    region = [f for f in changed if _in_regions(PurePosixPath(f), spec.regions)]
    other = [f for f in changed if f not in region]
    if region:
        return {"verdict": "re-run", "baseline": base, "head": head,
                "ahead": n_ahead, "region_churn": region, "other_churn": other,
                "reason": f"{len(region)} file(s) changed under the editable "
                          f"regions — the judged code no longer exists as judged"}
    return {"verdict": "still-current", "baseline": base, "head": head,
            "ahead": n_ahead, "region_churn": [], "other_churn": other,
            "reason": f"head is {n_ahead} commit(s) ahead but the editable "
                      f"regions are untouched — the campaign's claim stands"
                      + (f" ({len(other)} out-of-region file(s) changed: deps/"
                         f"sibling churn can still shift perf)" if other else "")}


_NEXT = """next steps (re-run path):
  1. bump target_repo.baseline_ref in the spec to the new head
  2. re-derive the differential DIFF hash on the new baseline (run the probe)
  3. re-run L1 (`aro sweep`) and eyeball the frontier before igniting a campaign
The permanent ledger keys nodes by baseline fingerprint — old judgments stay
valid history; the new baseline simply opens new nodes."""


def cli(args) -> None:
    from . import spec as specmod
    sp = specmod.load(args.spec)
    a = assess(sp, ref=args.ref)
    if args.json:
        print(json.dumps(a, indent=1))
        return
    short = (lambda s: (s or "?")[:12])
    print(f"recheck {sp.name}: baseline {short(a['baseline'])} vs {args.ref} "
          f"{short(a['head'])}"
          + (f" ({a['ahead']} commit(s) ahead)" if a.get("ahead") else ""))
    for f in a["region_churn"][:20]:
        print(f"  region churn: {f}")
    if len(a["region_churn"]) > 20:
        print(f"  ... and {len(a['region_churn']) - 20} more")
    print(f"verdict: {a['verdict'].upper()} — {a['reason']}")
    if a["verdict"] == "re-run":
        print(_NEXT)
