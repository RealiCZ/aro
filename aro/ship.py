"""`aro ship` family — clearance before packaging + quality proof before opening.

``aro ship gate`` — clearance check before packaging a PR from certified edits.
The terminal stamp certifies criterion-Ir wins against a specific baseline sha.
The PR targets some remote branch head. Those two must agree: otherwise the
operator is shipping never-replayed bytes (mega-evm PR #346 — certified on X,
hand-rebased onto Y after main moved under an editable region).

Gate fails closed:

  - no mergeable entries → nothing to ship
  - mergeable stamp lacks ``baseline_sha`` → re-measure with current aro
  - mixed baseline_sha across mergeable stamps → integrity error
  - stamp baseline ≠ target head → print re-certification steps, exit 1
  - ``git fetch`` failure → exit 1 (never silently pass on network failure)

``aro ship conformance`` — machine record of target-repo quality checks on the
final PR-branch checkout (fmt / clippy / test / …). Prose steps in run-to-pr
§3/§4 were skipped twice (#346 failing test, #347 zero tests + fmt drift);
this command exits non-zero on any failure and binds the record to ``head_sha``.
See ``ship_conformance`` in ``skill/references/spec-slots.md``.

Gate = baseline currency before packaging; conformance = quality proof on the
final branch before opening the PR. Gate is read-only on campaign artifacts;
conformance writes only the conformance record file.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import vcs
from .spec import spec_field

DEFAULT_SHIP_TARGET = "origin/main"
DEFAULT_CONFORMANCE_TIMEOUT_S = 1800
DEFAULT_CONFORMANCE_OUT_NAME = ".aro-conformance.json"
CONFORMANCE_TAIL_LINES = 40
# GNU timeout convention; non-zero so all_green is false.
TIMEOUT_EXIT = 124

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


def resolve_ship_conformance(spec) -> list:
    """Return the ``ship_conformance`` list from the spec, or ``[]`` if unset.

    Fail-closed callers treat empty as "spec defines no ship_conformance".
    """
    items = spec_field(spec, "ship_conformance", default=None)
    if items is None:
        raw = getattr(spec, "raw", None) or {}
        items = raw.get("ship_conformance") if isinstance(raw, dict) else None
    if not items:
        return []
    if not isinstance(items, (list, tuple)):
        raise ValueError(
            f"ship_conformance must be a list of {{name, cmd}} objects, "
            f"got {type(items).__name__}")
    return list(items)


def workdir_has_tracked_dirt(workdir) -> bool:
    """True when ``workdir`` has uncommitted changes to tracked files.

    Untracked / ignored paths (``??`` / ``!!``) do not count: the record binds
    to committed bytes only; writing ``.aro-conformance.json`` itself is fine.
    """
    text = vcs.status_porcelain(workdir)
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("??") or line.startswith("!!"):
            continue
        return True
    return False


def _combined_tail(stdout, stderr, n: int = CONFORMANCE_TAIL_LINES) -> str:
    parts = []
    if stdout:
        parts.append(stdout if isinstance(stdout, str)
                     else stdout.decode("utf-8", "replace"))
    if stderr:
        parts.append(stderr if isinstance(stderr, str)
                     else stderr.decode("utf-8", "replace"))
    combined = "\n".join(parts)
    lines = combined.splitlines()
    return "\n".join(lines[-n:])


def run_conformance_check(workdir, item: dict, *,
                          default_timeout: float = DEFAULT_CONFORMANCE_TIMEOUT_S
                          ) -> dict:
    """Run one shell check in ``workdir``. Always returns a check dict."""
    name = str(item.get("name") or "?").strip() or "?"
    cmd = item.get("cmd")
    if not cmd or not str(cmd).strip():
        return {
            "name": name,
            "cmd": str(cmd or ""),
            "exit": 1,
            "duration_s": 0.0,
            "tail": "missing cmd",
        }
    cmd = str(cmd)
    raw_to = item.get("timeout_s", default_timeout)
    try:
        timeout = float(raw_to)
    except (TypeError, ValueError):
        timeout = float(default_timeout)
    if timeout <= 0:
        timeout = float(default_timeout)

    t0 = time.monotonic()
    try:
        cp = subprocess.run(
            cmd, shell=True, cwd=str(workdir),
            capture_output=True, text=True, timeout=timeout)
        duration = time.monotonic() - t0
        return {
            "name": name,
            "cmd": cmd,
            "exit": int(cp.returncode),
            "duration_s": round(duration, 3),
            "tail": _combined_tail(cp.stdout, cp.stderr),
        }
    except subprocess.TimeoutExpired as e:
        duration = time.monotonic() - t0
        tail = _combined_tail(e.stdout, e.stderr)
        msg = f"TIMEOUT after {timeout:g}s"
        tail = f"{tail}\n{msg}".strip() if tail else msg
        return {
            "name": name,
            "cmd": cmd,
            "exit": TIMEOUT_EXIT,
            "duration_s": round(duration, 3),
            "tail": tail,
        }


def _print_conformance_table(checks: list, *, file) -> None:
    name_w = max((len(c.get("name") or "") for c in checks), default=4)
    name_w = max(name_w, 4)
    print(f"  {'name':<{name_w}}  {'exit':>4}  {'duration_s':>10}  verdict",
          file=file)
    for c in checks:
        n = c.get("name") or "?"
        ex = c.get("exit")
        dur = c.get("duration_s")
        try:
            dur_s = f"{float(dur):.3f}"
        except (TypeError, ValueError):
            dur_s = str(dur)
        verdict = "PASS" if ex == 0 else "FAIL"
        print(f"  {n:<{name_w}}  {ex!s:>4}  {dur_s:>10}  {verdict}", file=file)


def conformance(spec, workdir, *,
                out_path=None,
                spec_path: Optional[str] = None,
                file=None) -> int:
    """Run ship_conformance checks in ``workdir``; write the record.

    Returns process exit code (0 all-green / 1 any failure or preflight error).
    Always writes the record when checks were attempted (including mixed fails).
    Preflight errors (no checks defined, not a git checkout, dirty tree) exit 1
    without writing a record.
    """
    out = file if file is not None else sys.stdout
    err = sys.stderr
    workdir = Path(workdir)

    try:
        items = resolve_ship_conformance(spec)
    except ValueError as e:
        print(f"ship conformance ERROR: {e}", file=err)
        return 1

    if not items:
        print(
            "ship conformance FAIL: spec defines no ship_conformance — "
            "define it (see spec-slots.md)",
            file=out)
        return 1

    if not workdir.is_dir():
        print(f"ship conformance ERROR: workdir is not a directory: {workdir}",
              file=err)
        return 1

    head_sha = vcs.rev_parse(workdir, "HEAD")
    if not head_sha:
        print(
            f"ship conformance ERROR: workdir is not a git checkout "
            f"(git rev-parse HEAD failed): {workdir}",
            file=err)
        return 1

    try:
        dirty = workdir_has_tracked_dirt(workdir)
    except RuntimeError as e:
        print(f"ship conformance ERROR: git status failed: {e}", file=err)
        return 1
    if dirty:
        print(
            "ship conformance FAIL: workdir has uncommitted tracked changes — "
            "record must describe committed bytes only "
            f"(head={head_sha[:12]})",
            file=out)
        return 1

    checks = [run_conformance_check(workdir, item) for item in items]
    all_green = all(c.get("exit") == 0 for c in checks)

    record = {
        "head_sha": head_sha,
        "workdir": str(workdir.resolve()),
        "spec": (str(spec_path) if spec_path is not None
                 else getattr(spec, "name", "")),
        "checks": checks,
        "all_green": all_green,
    }

    dest = Path(out_path) if out_path else (workdir / DEFAULT_CONFORMANCE_OUT_NAME)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(record, indent=2) + "\n")
    except OSError as e:
        print(f"ship conformance ERROR: failed to write record {dest}: {e}",
              file=err)
        return 1

    if all_green:
        print("ship conformance PASS — all checks green", file=out)
        print(f"  head_sha:  {head_sha}", file=out)
        print(f"  record:    {dest}", file=out)
        print(f"  checks:    {len(checks)}", file=out)
        return 0

    print("ship conformance FAIL — one or more checks failed", file=out)
    print(f"  head_sha:  {head_sha}", file=out)
    print(f"  record:    {dest}", file=out)
    _print_conformance_table(checks, file=out)
    return 1


def cli(args) -> None:
    """`aro ship gate|conformance …`."""
    from . import spec as specmod

    action = getattr(args, "ship_action", None) or "gate"

    try:
        sp = specmod.load(args.spec)
    except Exception:
        raw = json.loads(Path(args.spec).read_text())
        sp = specmod.from_dict(raw)

    if action == "gate":
        code = gate(
            sp, args.manifest,
            target=getattr(args, "target", None),
            no_fetch=bool(getattr(args, "no_fetch", False)),
        )
        raise SystemExit(code)

    if action == "conformance":
        code = conformance(
            sp, args.workdir,
            out_path=getattr(args, "out", None),
            spec_path=args.spec,
        )
        raise SystemExit(code)

    raise SystemExit(f"unknown ship action {action!r}")
