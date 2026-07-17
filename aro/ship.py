"""`aro ship gate` — clearance check before packaging a PR from certified edits.

The terminal stamp certifies criterion-Ir wins against a specific baseline sha.
The PR targets some remote branch head. Those two must agree: otherwise the
operator is shipping never-replayed bytes (mega-evm PR #346 — certified on X,
hand-rebased onto Y after main moved under an editable region).

This gate fails closed:

  - no mergeable entries → nothing to ship
  - mergeable stamp lacks ``baseline_sha`` → re-measure with current aro
  - mixed baseline_sha across mergeable stamps → integrity error
  - stamp baseline ≠ target head → print re-certification steps, exit 1
  - ``git fetch`` failure → exit 1 (never silently pass on network failure)

Read-only on the campaign artifacts; never mutates the manifest or the spec.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from . import vcs
from .spec import spec_field

DEFAULT_SHIP_TARGET = "origin/main"

LEGACY_STAMP_MSG = (
    "stamp predates baseline recording — re-measure with current aro"
)

_RECERT = """re-certification required (do NOT hand-rebase certified edits):
  1. update the spec's baseline_ref to the new head sha
  2. aro recheck candidates --spec <spec> --out <run> [--baseline <new-sha>]
     (full-chain replay; unappliable orders drop out)
  3. aro terminal <spec> --baseline <wt> --candidate <wt> --orders <survivors>
     then --rejudge <terminal.json> --update-manifest <run>
never hand-rebase certified edits onto a moved baseline (#346)."""


def load_manifest(path) -> dict:
    """Load a manifest from a file path or a run dir containing manifest.json."""
    p = Path(path)
    if p.is_dir():
        p = p / "manifest.json"
    if not p.is_file():
        raise FileNotFoundError(f"no manifest at {p}")
    return json.loads(p.read_text())


def resolve_ship_target(spec, cli_target: Optional[str] = None) -> str:
    """CLI ``--target`` wins, else spec ``ship_target``, else ``origin/main``."""
    if cli_target and str(cli_target).strip():
        return str(cli_target).strip()
    st = spec_field(spec, "ship_target", default=None)
    if st and str(st).strip():
        return str(st).strip()
    raw = getattr(spec, "raw", None) or {}
    if isinstance(raw, dict):
        st = raw.get("ship_target")
        if st and str(st).strip():
            return str(st).strip()
    return DEFAULT_SHIP_TARGET


def split_remote_branch(ref: str) -> tuple:
    """Split ``remote/branch`` (branch may contain slashes)."""
    ref = str(ref).strip()
    if "/" not in ref:
        raise ValueError(
            f"ship target {ref!r} must be remote/branch (e.g. origin/main)")
    remote, branch = ref.split("/", 1)
    if not remote or not branch:
        raise ValueError(
            f"ship target {ref!r} must be remote/branch (e.g. origin/main)")
    return remote, branch


def resolve_target_head(repo, target_ref: str, *, no_fetch: bool = False) -> str:
    """Resolve the ship-target head sha in ``repo``.

    Default: ``git fetch <remote> <branch>`` then rev-parse ``FETCH_HEAD``.
    ``--no-fetch``: resolve the local ref only. Fetch failure raises RuntimeError.
    """
    repo = Path(repo)
    if no_fetch:
        sha = vcs.rev_parse(repo, target_ref)
        if not sha:
            raise RuntimeError(
                f"ship gate: ref {target_ref!r} does not resolve in {repo} "
                f"(try without --no-fetch, or git fetch first)")
        return sha
    try:
        remote, branch = split_remote_branch(target_ref)
    except ValueError as e:
        raise RuntimeError(f"ship gate: {e}") from e
    out = vcs.git(repo, "fetch", remote, branch, timeout=vcs.GIT_TIMEOUT)
    if out.returncode != 0:
        err = (out.stderr or out.stdout or "").strip() or f"rc={out.returncode}"
        raise RuntimeError(
            f"ship gate: git fetch {remote} {branch} failed — {err[:300]}")
    sha = vcs.rev_parse(repo, "FETCH_HEAD")
    if not sha:
        raise RuntimeError(
            f"ship gate: FETCH_HEAD did not resolve after "
            f"git fetch {remote} {branch}")
    return sha


def collect_mergeable_baselines(manifest: dict) -> dict:
    """Inspect mergeable entries' terminal stamps for baseline agreement.

    Returns a dict with keys:
      - mergeable: list of mergeable accepted entries
      - baseline_shas: set of non-empty baseline_sha values seen
      - missing: True when any mergeable stamp lacks baseline_sha
      - mixed: True when more than one distinct baseline_sha is present
      - stamp_sha256: a representative stamp sha256 (if any recorded)
    """
    mergeable = [a for a in (manifest.get("accepted") or [])
                 if a.get("mergeable")]
    shas = set()
    missing = False
    stamp_sha = None
    for a in mergeable:
        stamp = a.get("terminal_stamp")
        if not isinstance(stamp, dict):
            missing = True
            continue
        bsha = stamp.get("baseline_sha")
        if not bsha:
            missing = True
        else:
            shas.add(str(bsha))
        if stamp_sha is None and stamp.get("sha256"):
            stamp_sha = stamp.get("sha256")
    return {
        "mergeable": mergeable,
        "baseline_shas": shas,
        "missing": missing,
        "mixed": len(shas) > 1,
        "stamp_sha256": stamp_sha,
    }


def gate(spec, manifest_path, *,
         target: Optional[str] = None,
         no_fetch: bool = False,
         file=None) -> int:
    """Run the ship baseline gate. Returns process exit code (0 pass / 1 fail)."""
    out = file if file is not None else sys.stdout
    err = sys.stderr

    try:
        manifest = load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ship gate ERROR: failed to load manifest: {e}", file=err)
        return 1

    info = collect_mergeable_baselines(manifest)
    n_merge = len(info["mergeable"])
    if n_merge == 0:
        print("ship gate FAIL: nothing to ship (no mergeable:true entries)",
              file=out)
        return 1

    if info["missing"]:
        print(f"ship gate FAIL: {LEGACY_STAMP_MSG}", file=out)
        print(f"  mergeable entries: {n_merge}", file=out)
        return 1

    if info["mixed"]:
        print("ship gate FAIL: mixed terminal_stamp.baseline_sha across "
              "mergeable entries (integrity error)", file=out)
        for s in sorted(info["baseline_shas"]):
            print(f"  baseline_sha: {s}", file=out)
        return 1

    stamp_baseline = next(iter(info["baseline_shas"]))
    target_ref = resolve_ship_target(spec, target)
    try:
        head = resolve_target_head(spec.repo, target_ref, no_fetch=no_fetch)
    except RuntimeError as e:
        print(f"ship gate ERROR: {e}", file=err)
        return 1

    short = (lambda s: (s or "?")[:12])
    if stamp_baseline == head:
        print("ship gate PASS — clearance to package PR", file=out)
        print(f"  mergeable:     {n_merge}", file=out)
        if info["stamp_sha256"]:
            print(f"  stamp sha256:  {info['stamp_sha256']}", file=out)
        print(f"  baseline_sha:  {stamp_baseline}", file=out)
        print(f"  target:        {target_ref} → {head}", file=out)
        return 0

    print("ship gate FAIL: stamp baseline ≠ ship target head", file=out)
    print(f"  mergeable:     {n_merge}", file=out)
    print(f"  stamp baseline:{stamp_baseline}", file=out)
    print(f"  target:        {target_ref} → {head}", file=out)
    print(f"  (stamp {short(stamp_baseline)} vs head {short(head)})", file=out)
    # Best-effort region-churn context (same signal as aro recheck staleness).
    try:
        from . import recheck as recheckmod
        a = recheckmod.assess(spec, ref=head)
        print(f"  recheck:       {a.get('verdict')} — {a.get('reason')}",
              file=out)
        for fpath in (a.get("region_churn") or [])[:10]:
            print(f"    region churn: {fpath}", file=out)
    except Exception as e:
        print(f"  recheck:       (unavailable: {e})", file=out)
    print(_RECERT, file=out)
    return 1


def cli(args) -> None:
    """`aro ship gate <spec> --manifest <out_dir|manifest.json>`."""
    from . import spec as specmod

    action = getattr(args, "ship_action", None) or "gate"
    if action != "gate":
        raise SystemExit(f"unknown ship action {action!r}")

    try:
        sp = specmod.load(args.spec)
    except Exception:
        raw = json.loads(Path(args.spec).read_text())
        sp = specmod.from_dict(raw)

    code = gate(
        sp, args.manifest,
        target=getattr(args, "target", None),
        no_fetch=bool(getattr(args, "no_fetch", False)),
    )
    raise SystemExit(code)
