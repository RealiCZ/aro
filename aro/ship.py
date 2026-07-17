"""`aro ship` family — clearance before packaging + quality proof before opening
+ PR-outcome feedback into the campaign loop.

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

``aro ship package`` — inline gate + worktree at the certified head + apply
mergeable patches in acceptance order + single certified-set commit + write
``pr_body.md``. Fail-closed on apply mismatch (integrity error vs the stamp).

``aro ship conformance`` — machine record of target-repo quality checks on the
final PR-branch checkout (fmt / clippy / test / …). Prose steps in run-to-pr
§3/§4 were skipped twice (#346 failing test, #347 zero tests + fmt drift);
this command exits non-zero on any failure and binds the record to ``head_sha``.
See ``ship_conformance`` in ``skill/references/spec-slots.md``.

``aro ship open`` — fail-closed machine gate before ``git push`` + ``gh pr
create``: re-gate, green conformance record bound to current HEAD, clean tree,
post-cert commit whitelist, branch ≠ ship-target. Opening a PR without a green
record is impossible, not just forbidden. ``git push`` and ``gh`` go through
injectable runner seams so tests never touch the network.

``aro ship watch`` — one-shot poll of an opened PR's outcome (operator/cron;
NOT a daemon). Merged → stamp ``shipped`` on mergeable entries (campaign
ledger). Closed unmerged or CHANGES_REQUESTED → harvest review feedback into
``pr_feedback/`` + seed ``reattempt-queue.json`` for the next campaign.
``gh`` goes through an injectable runner seam so tests never touch the network;
a failing ``gh`` call exits 1 (never silent no-op).

Gate = baseline currency before packaging; package = certified branch + PR body;
conformance = quality proof on the final branch before opening; open = push + PR
only when every machine check passes; watch = outcome feedback after the PR
exists. Gate is read-only on campaign artifacts; package writes a worktree +
body; conformance writes only the conformance record; open pushes/creates the
PR; watch may stamp ``shipped`` and write harvest/queue files.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import vcs
from .spec import spec_field
from .types import Patch

DEFAULT_SHIP_TARGET = "origin/main"
DEFAULT_SHIP_REMOTE = "origin"
DEFAULT_CONFORMANCE_TIMEOUT_S = 1800
DEFAULT_CONFORMANCE_OUT_NAME = ".aro-conformance.json"
PR_BODY_NAME = "pr_body.md"
CONFORMANCE_TAIL_LINES = 40
# GNU timeout convention; non-zero so all_green is false.
TIMEOUT_EXIT = 124

# Fields requested from ``gh pr view --json``. Adjust if gh drops/renames any;
# reviewComments come from a second ``gh api`` call (inline path binding).
GH_PR_VIEW_FIELDS = (
    "state,mergedAt,mergeCommit,reviews,comments,reviewDecision,headRefOid,url"
)
GH_TIMEOUT_S = 60
PUSH_TIMEOUT_S = 120
REATTEMPT_QUEUE_NAME = "reattempt-queue.json"
PR_FEEDBACK_DIR = "pr_feedback"

# Post-certification commit subjects allowed after the certified-set commit
# (pr-discipline dual-green tests + mechanical cargo fmt).
_POST_CERT_SUBJECT = re.compile(
    r"^(test(\(|:)|style: cargo fmt)"
)
_CERTIFIED_SUBJECT = re.compile(r"^perf: ARO \d+-edit certified set\b")
_TRADED_NOTE = re.compile(
    r"traded:\s*(\S+)\s+([+\-]?\d+(?:\.\d+)?)%\s+\(cap\s+([+\-]?\d+(?:\.\d+)?)%\)"
)
_UNSAFE_RE = re.compile(r"\bunsafe\b")

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


# ---------------------------------------------------------------------------
# ship watch — PR outcome → campaign ledger / re-attempt seeds
# ---------------------------------------------------------------------------

# CompletedProcess-like: .returncode, .stdout, .stderr
GhRunner = Callable[[list], "subprocess.CompletedProcess"]


def default_gh_runner(argv: list) -> subprocess.CompletedProcess:
    """Invoke ``gh`` with *argv* (no leading ``gh``). Network; not for tests."""
    return subprocess.run(
        ["gh", *argv],
        capture_output=True, text=True, timeout=GH_TIMEOUT_S,
    )


def resolve_run_and_manifest(path) -> tuple:
    """Return ``(run_dir, manifest_path)`` for a run dir or manifest.json path."""
    p = Path(path)
    if p.is_dir():
        man = p / "manifest.json"
        if not man.is_file():
            raise FileNotFoundError(f"no manifest at {man}")
        return p, man
    if not p.is_file():
        raise FileNotFoundError(f"no manifest at {p}")
    return p.parent, p


def pr_feedback_key(pr_ref: str) -> str:
    """Stable filename stem for a PR ref (number, URL, or owner/name#N)."""
    s = str(pr_ref).strip()
    m = re.search(r"/pull/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"#(\d+)\s*$", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d+", s):
        return s
    # Fallback: sanitize for a path component.
    safe = re.sub(r"[^\w.\-]+", "_", s).strip("_")
    return safe or "pr"


def hint_hash(hint: str) -> str:
    """Short content hash for reattempt-queue dedup."""
    return hashlib.sha256((hint or "").encode("utf-8")).hexdigest()[:16]


def _author_login(obj) -> str:
    if not isinstance(obj, dict):
        return ""
    a = obj.get("author")
    if isinstance(a, dict):
        return str(a.get("login") or a.get("name") or "")
    if isinstance(a, str):
        return a
    return ""


def _merge_sha_from_payload(payload: dict) -> Optional[str]:
    mc = payload.get("mergeCommit")
    if isinstance(mc, dict):
        return mc.get("oid")  # gh: {"oid": "..."}
    if isinstance(mc, str) and mc:
        return mc
    return None


def classify_pr_verdict(payload: dict) -> str:
    """Return one of: merged | closed | changes_requested | open.

    Precedence: MERGED (or CLOSED with merge evidence) → CLOSED unmerged →
    OPEN + CHANGES_REQUESTED → open (no-op).
    """
    state = str(payload.get("state") or "").upper()
    merged_at = payload.get("mergedAt")
    merge_sha = _merge_sha_from_payload(payload)
    if state == "MERGED":
        return "merged"
    if state == "CLOSED" and (merged_at or merge_sha):
        return "merged"
    if state == "CLOSED":
        return "closed"
    decision = str(payload.get("reviewDecision") or "").upper()
    if state == "OPEN" and decision == "CHANGES_REQUESTED":
        return "changes_requested"
    return "open"


def _entry_files(entry: dict) -> list:
    files = entry.get("files") or []
    if not isinstance(files, list):
        return []
    return [str(f) for f in files if f]


def bind_entry_for_path(manifest: dict, path: Optional[str]) -> Optional[dict]:
    """Best-effort: first accepted entry whose ``files`` touch *path* (prefix).

    Matching: exact path, or either side is a prefix of the other (handles
    repo-relative vs crate-relative comments). Returns ``{order, fn}`` or None.
    """
    if not path:
        return None
    path = str(path).lstrip("./")
    for a in manifest.get("accepted") or []:
        for f in _entry_files(a):
            f = str(f).lstrip("./")
            if path == f or path.startswith(f + "/") or f.startswith(path + "/"):
                return {"order": a.get("order"), "fn": a.get("fn")}
            # basename-level last resort when paths share a suffix
            if path.endswith("/" + f) or f.endswith("/" + path):
                return {"order": a.get("order"), "fn": a.get("fn")}
    return None


def harvest_feedback_items(payload: dict, manifest: dict) -> list:
    """Normalize reviews + top-level comments + inline review comments."""
    items = []

    for rc in payload.get("reviewComments") or []:
        if not isinstance(rc, dict):
            continue
        path = rc.get("path")
        body = rc.get("body") or ""
        entry = bind_entry_for_path(manifest, path)
        items.append({
            "kind": "review_comment",
            "author": _author_login(rc),
            "body": body,
            "path": path,
            "original_position": rc.get("original_position") or rc.get(
                "originalPosition"),
            "original_line": rc.get("original_line") or rc.get("originalLine"),
            "line": rc.get("line"),
            "entry": entry,
        })

    for rev in payload.get("reviews") or []:
        if not isinstance(rev, dict):
            continue
        body = rev.get("body") or ""
        # Empty-body reviews (approve click only) still recorded for provenance.
        items.append({
            "kind": "review",
            "author": _author_login(rev),
            "body": body,
            "path": None,
            "state": rev.get("state"),
            "entry": None,
        })

    for c in payload.get("comments") or []:
        if not isinstance(c, dict):
            continue
        items.append({
            "kind": "comment",
            "author": _author_login(c),
            "body": c.get("body") or "",
            "path": None,
            "entry": None,
        })

    return items


def _owner_repo_number(pr_ref: str, payload: dict) -> Optional[tuple]:
    """Parse (owner, repo, number) from payload url or pr_ref."""
    url = str(payload.get("url") or pr_ref or "")
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.search(r"^([^/]+)/([^/#]+)#(\d+)$", str(pr_ref).strip())
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def fetch_pr_payload(pr_ref: str, *, gh_runner: GhRunner) -> dict:
    """Poll PR state via ``gh``. Raises RuntimeError on non-zero exit / bad JSON.

    Primary call: ``gh pr view <ref> --json <fields>``.
    Secondary (best-effort): ``gh api repos/.../pulls/.../comments`` for inline
    review comments (path binding). Secondary failure does not fail the poll —
    only the primary ``pr view`` is mandatory.
    """
    cp = gh_runner([
        "pr", "view", str(pr_ref), "--json", GH_PR_VIEW_FIELDS,
    ])
    if getattr(cp, "returncode", 1) != 0:
        err = (getattr(cp, "stderr", None) or getattr(cp, "stdout", None)
               or "").strip()
        raise RuntimeError(
            f"gh pr view failed (rc={getattr(cp, 'returncode', '?')}): "
            f"{err[:400] or 'no output'}")
    try:
        payload = json.loads(cp.stdout or "")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh pr view returned non-JSON: {e}") from e
    if not isinstance(payload, dict):
        raise RuntimeError("gh pr view JSON root must be an object")

    # Inline review comments (path-bearing) — optional second call.
    if "reviewComments" not in payload:
        loc = _owner_repo_number(pr_ref, payload)
        if loc:
            owner, repo, num = loc
            api = gh_runner([
                "api",
                f"repos/{owner}/{repo}/pulls/{num}/comments",
                "--paginate",
            ])
            if getattr(api, "returncode", 1) == 0 and api.stdout:
                try:
                    rcs = json.loads(api.stdout)
                    if isinstance(rcs, list):
                        payload["reviewComments"] = rcs
                except json.JSONDecodeError:
                    pass
        if "reviewComments" not in payload:
            payload["reviewComments"] = []
    return payload


def stamp_shipped(manifest: dict, *, pr: str, merge_sha: Optional[str]) -> int:
    """Upsert ``shipped`` on every mergeable entry. Returns count stamped."""
    n = 0
    stamp = {
        "pr": pr,
        "state": "merged",
        "merge_sha": merge_sha,
    }
    for a in manifest.get("accepted") or []:
        if not a.get("mergeable"):
            continue
        a["shipped"] = dict(stamp)
        n += 1
    return n


def write_manifest(manifest_path: Path, manifest: dict) -> None:
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def write_feedback_file(run_dir: Path, pr_key: str, doc: dict) -> Path:
    """Overwrite ``<run>/pr_feedback/<pr_key>.json`` (idempotent harvest)."""
    dest_dir = run_dir / PR_FEEDBACK_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{pr_key}.json"
    dest.write_text(json.dumps(doc, indent=2) + "\n")
    return dest


def append_reattempt_queue(run_dir: Path, seeds: list) -> tuple:
    """Append seeds to ``reattempt-queue.json``; dedup by (pr, order, hint-hash).

    Returns ``(path, n_added, n_skipped_dup)``.
    """
    path = run_dir / REATTEMPT_QUEUE_NAME
    existing = []
    if path.is_file():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, list):
                existing = raw
        except (OSError, json.JSONDecodeError):
            existing = []

    seen = set()
    for row in existing:
        if not isinstance(row, dict):
            continue
        seen.add((
            str(row.get("pr") or ""),
            row.get("order"),
            hint_hash(str(row.get("hint") or "")),
        ))

    added = 0
    skipped = 0
    for s in seeds:
        key = (
            str(s.get("pr") or ""),
            s.get("order"),
            hint_hash(str(s.get("hint") or "")),
        )
        if key in seen:
            skipped += 1
            continue
        existing.append(s)
        seen.add(key)
        added += 1

    path.write_text(json.dumps(existing, indent=2) + "\n")
    return path, added, skipped


def reattempt_seeds_from_items(items: list, *, pr: str) -> list:
    """One pending seed per bound feedback item (entry is non-null)."""
    seeds = []
    for it in items:
        entry = it.get("entry") if isinstance(it, dict) else None
        if not isinstance(entry, dict) or entry.get("order") is None:
            continue
        seeds.append({
            "order": entry.get("order"),
            "fn": entry.get("fn"),
            "hint": it.get("body") or "",
            "pr": pr,
            "status": "pending",
        })
    return seeds


# ---------------------------------------------------------------------------
# ship package — certified branch + PR body
# ---------------------------------------------------------------------------

def resolve_ship_remote(spec) -> str:
    """Spec optional ``ship_remote``; default ``origin``."""
    r = spec_field(spec, "ship_remote", default=None)
    if r and str(r).strip():
        return str(r).strip()
    raw = getattr(spec, "raw", None) or {}
    if isinstance(raw, dict):
        r = raw.get("ship_remote")
        if r and str(r).strip():
            return str(r).strip()
    return DEFAULT_SHIP_REMOTE


def resolve_pr_labels(spec) -> list:
    """Spec optional ``pr_labels`` list; default ``[]``."""
    labels = spec_field(spec, "pr_labels", default=None)
    if labels is None:
        raw = getattr(spec, "raw", None) or {}
        labels = raw.get("pr_labels") if isinstance(raw, dict) else None
    if not labels:
        return []
    if not isinstance(labels, (list, tuple)):
        raise ValueError(
            f"pr_labels must be a list of strings, got {type(labels).__name__}")
    return [str(x) for x in labels if str(x).strip()]


def resolve_control_lanes(spec) -> list:
    """Control-lane row keys from the spec (excluded from PR Delta headlines)."""
    lanes = getattr(spec, "control_lanes", None)
    if lanes is None:
        raw = getattr(spec, "raw", None) or {}
        lanes = raw.get("control_lanes") if isinstance(raw, dict) else None
    if not lanes:
        return []
    return [str(x) for x in lanes]


def mergeable_entries(manifest: dict) -> list:
    """``mergeable:true`` accepted entries sorted by acceptance ``order``."""
    rows = [a for a in (manifest.get("accepted") or []) if a.get("mergeable")]
    rows.sort(key=lambda a: (a.get("order") is None, a.get("order") or 0))
    return rows


def default_package_workdir(spec, run_name: str) -> Path:
    """Same parent as ``SpecTarget.make_worktree``: ``<repo.parent>/.aro-worktrees/``."""
    parent = Path(spec.repo).resolve().parent / ".aro-worktrees"
    parent.mkdir(parents=True, exist_ok=True)
    return parent / f"ship-{run_name}"


def default_package_branch(run_name: str) -> str:
    return f"aro/ship-{run_name}"


def _apply_patch_to_work(patch: Patch, work: Path) -> None:
    """SpecTarget.apply semantics: unique SEARCH per edit, exact replace."""
    if patch.is_noop:
        return
    for e in patch.edits:
        f = Path(work) / e.path
        if not f.is_file():
            raise RuntimeError(f"search text not found in {e.path} (file missing)")
        content = f.read_text()
        count = content.count(e.search)
        if count != 1:
            what = ("not found" if count == 0
                    else f"found {count}x (must be unique)")
            raise RuntimeError(f"search text {what} in {e.path}")
        idx = content.find(e.search)
        f.write_text(content[:idx] + e.replace + content[idx + len(e.search):])


def _terminal_doc_for_body(manifest: dict, mergeable: list, run_dir: Path) -> dict:
    """Best-effort terminal evidence for the PR body (stamp source or entry fields)."""
    doc: dict = {}
    # Prefer loading the stamp source file (full notes / measured_orders).
    for a in mergeable:
        stamp = a.get("terminal_stamp") if isinstance(a, dict) else None
        if not isinstance(stamp, dict):
            continue
        src = stamp.get("source")
        if not src:
            continue
        p = Path(src)
        if not p.is_file():
            p = run_dir / src
        if p.is_file():
            try:
                loaded = json.loads(p.read_text())
                if isinstance(loaded, dict):
                    return loaded
            except (OSError, json.JSONDecodeError):
                pass
    term = manifest.get("terminal")
    if isinstance(term, dict):
        doc = dict(term)
    # Overlay entry-level fields when terminal block is thin.
    for a in mergeable:
        if a.get("bench_ir_rows") and "bench_ir_rows" not in doc:
            doc["bench_ir_rows"] = dict(a["bench_ir_rows"])
        if a.get("profile_fingerprint") and "profile_fingerprint" not in doc:
            doc["profile_fingerprint"] = a.get("profile_fingerprint")
        stamp = a.get("terminal_stamp") if isinstance(a, dict) else None
        if isinstance(stamp, dict):
            doc.setdefault("verdict", stamp.get("verdict"))
            doc.setdefault("baseline_sha", stamp.get("baseline_sha"))
            doc.setdefault("sha256", stamp.get("sha256"))
            doc.setdefault("source", stamp.get("source"))
        break
    return doc


def _stamp_fields(mergeable: list) -> dict:
    """Representative stamp fields shared across mergeable entries (gate-agreed)."""
    out = {
        "verdict": None,
        "sha256": None,
        "baseline_sha": None,
        "source": None,
    }
    for a in mergeable:
        stamp = a.get("terminal_stamp") if isinstance(a, dict) else None
        if not isinstance(stamp, dict):
            continue
        if out["verdict"] is None:
            out["verdict"] = stamp.get("verdict") or a.get("terminal")
        if out["sha256"] is None:
            out["sha256"] = stamp.get("sha256")
        if out["baseline_sha"] is None:
            out["baseline_sha"] = stamp.get("baseline_sha")
        if out["source"] is None:
            out["source"] = stamp.get("source")
        if all(out.values()):
            break
    if out["verdict"] is None and mergeable:
        out["verdict"] = mergeable[0].get("terminal") or "TERMINAL_CONFIRMED"
    return out


def _parse_traded_notes(notes) -> list:
    """Return list of ``{row, delta_pct, cap}`` from terminal notes."""
    rows = []
    for n in notes or []:
        m = _TRADED_NOTE.search(str(n))
        if not m:
            continue
        rows.append({
            "row": m.group(1),
            "delta_pct": m.group(2),
            "cap": m.group(3),
        })
    return rows


def generate_pr_body(spec, manifest: dict, *,
                     run_dir: Path,
                     run_name: str,
                     mergeable: list,
                     files_changed: list,
                     patch_texts: Optional[dict] = None) -> str:
    """Build the markdown PR body from certified-set evidence.

    Delta excludes control-lane rows (spec ``control_lanes``) so control drift
    never headlines the PR (#347 lesson).
    """
    stamp = _stamp_fields(mergeable)
    term = _terminal_doc_for_body(manifest, mergeable, run_dir)
    verdict = (stamp.get("verdict") or term.get("verdict")
               or "TERMINAL_CONFIRMED")
    baseline_sha = (stamp.get("baseline_sha") or term.get("baseline_sha")
                    or manifest.get("baseline_ref") or "?")
    stamp_sha = stamp.get("sha256") or term.get("sha256") or "?"
    stamp_src = stamp.get("source") or term.get("source") or "?"
    n = len(mergeable)
    control = set(resolve_control_lanes(spec))

    ir_rows = dict(term.get("bench_ir_rows") or {})
    if not ir_rows:
        for a in mergeable:
            if a.get("bench_ir_rows"):
                ir_rows = dict(a["bench_ir_rows"])
                break

    # Status counts from full row list when present; else infer from Δ map.
    n_improved = n_regressed = n_control = 0
    subject_deltas = []  # (abs_delta, key, delta)
    rows_detail = term.get("rows") or []
    if rows_detail:
        for r in rows_detail:
            if not isinstance(r, dict):
                continue
            key = str(r.get("row_key") or "")
            st = str(r.get("status") or "")
            dp = r.get("delta_pct")
            if key in control or st.startswith("control-"):
                n_control += 1
                continue
            if st == "improved":
                n_improved += 1
            elif st == "regressed":
                n_regressed += 1
            if isinstance(dp, (int, float)):
                subject_deltas.append((abs(float(dp)), key, float(dp)))
    else:
        for key, dp in ir_rows.items():
            if key in control:
                n_control += 1
                continue
            try:
                dpf = float(dp)
            except (TypeError, ValueError):
                continue
            subject_deltas.append((abs(dpf), key, dpf))
            if dpf < 0:
                n_improved += 1
            elif dpf > 0:
                n_regressed += 1
    subject_deltas.sort(key=lambda t: (-t[0], t[1]))

    lines = []
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"ARO certified set: **{verdict}**, **{n}** mergeable edit"
        f"{'' if n == 1 else 's'}, campaign `{run_name}`, "
        f"baseline `{baseline_sha}`.")
    lines.append("")
    lines.append("## Delta (Ir-first)")
    lines.append("")
    lines.append(
        f"Counts (subject / control): improved={n_improved}, "
        f"regressed={n_regressed}, control={n_control}. "
        f"Control-lane rows excluded from the table below "
        f"(lanes: {', '.join(sorted(control)) or 'none'}).")
    lines.append("")
    if subject_deltas:
        lines.append("| row | Δ% |")
        lines.append("|---|---:|")
        for _, key, dp in subject_deltas[:20]:
            lines.append(f"| `{key}` | {dp:+.4f}% |")
    else:
        lines.append("_(no subject-row Ir deltas recorded)_")
    lines.append("")

    traded = _parse_traded_notes(term.get("notes"))
    lines.append("## Traded regressions")
    lines.append("")
    if traded:
        lines.append("| row | Δ% | cap |")
        lines.append("|---|---:|---:|")
        for t in traded:
            lines.append(
                f"| `{t['row']}` | {t['delta_pct']}% | {t['cap']}% |")
    elif str(verdict) == "TERMINAL_CONFIRMED_WITH_TRADE":
        lines.append(
            "_(WITH_TRADE verdict but no `traded:` notes found in terminal "
            "evidence — re-check terminal.json)_")
    else:
        lines.append("_(none)_")
    lines.append("")

    lines.append("## Outlier disclosure")
    lines.append("")
    disclosed = [a for a in mergeable
                 if a.get("quarantine_disclosure") == "required"]
    if not disclosed:
        lines.append("_(none)_")
    else:
        for a in disclosed:
            fn = a.get("fn") or "?"
            dp = a.get("delta_pct")
            dp_s = (f"{float(dp):+.3f}%" if isinstance(dp, (int, float))
                    else str(dp))
            cleared = a.get("quarantine_cleared_by") or "?"
            lines.append(f"### `{fn}` (Δ {dp_s})")
            lines.append("")
            lines.append(f"- **cleared-by:** `{cleared}`")
            if cleared == "human-audit":
                audit = a.get("quarantine_audit") or {}
                if isinstance(audit, dict):
                    lines.append(f"  - by: {audit.get('by') or '?'}")
                    lines.append(f"  - date: {audit.get('date') or '?'}")
                    lines.append(
                        f"  - evidence: {audit.get('evidence') or '?'}")
            elif cleared == "auto-evidence":
                rev = a.get("reverify") or {}
                v = rev.get("verdict") if isinstance(rev, dict) else None
                lines.append(
                    f"  - mechanical evidence: "
                    f"`{v or 'reverify-pass'}`")
            # unsafe attention from patch text
            order = a.get("order")
            ptxt = (patch_texts or {}).get(order, "")
            if not ptxt and a.get("patch_path"):
                try:
                    ptxt = (run_dir / a["patch_path"]).read_text()
                except OSError:
                    ptxt = ""
            if _UNSAFE_RE.search(ptxt or ""):
                lines.append(
                    "- **review attention:** patch text contains `unsafe`")
            lines.append("")
    if disclosed and not lines[-1] == "":
        lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- **run:** `{run_name}`")
    lines.append(f"- **stamp source:** `{stamp_src}`")
    lines.append(f"- **stamp sha256:** `{stamp_sha}`")
    lines.append(f"- **baseline_sha:** `{baseline_sha}`")
    fp = (term.get("env_fingerprint") or term.get("profile_fingerprint")
          or next((a.get("profile_fingerprint") for a in mergeable
                   if a.get("profile_fingerprint")), None)
          or "?")
    lines.append(f"- **pinned_tools / fingerprint:** `{fp}`")
    measured = term.get("measured_orders")
    if measured is None:
        measured = [a.get("order") for a in mergeable]
    lines.append(f"- **measured orders:** {measured}")
    lines.append("")

    lines.append("## Files changed")
    lines.append("")
    if files_changed:
        for f in files_changed:
            lines.append(f"- `{f}`")
    else:
        lines.append("_(none)_")
    lines.append("")
    lines.append("---")
    lines.append("*This PR was generated by an automated agent.*")
    lines.append("")
    return "\n".join(lines)


def package(spec, manifest_path, *,
            target: Optional[str] = None,
            no_fetch: bool = False,
            branch: Optional[str] = None,
            workdir=None,
            file=None) -> int:
    """Build the certified PR branch + ``pr_body.md``. Exit 0 / 1.

    Always runs ``gate()`` first (same fetch/target semantics). Non-PASS aborts
    before any worktree mutation.
    """
    out = file if file is not None else sys.stdout
    err = sys.stderr

    # 1. Inline gate — no packaging without clearance.
    gate_code = gate(
        spec, manifest_path, target=target, no_fetch=no_fetch, file=out)
    if gate_code != 0:
        print("ship package ABORT: gate did not PASS", file=out)
        return 1

    try:
        run_dir, man_path = resolve_run_and_manifest(manifest_path)
        manifest = json.loads(man_path.read_text())
    except (OSError, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ship package ERROR: failed to load manifest: {e}", file=err)
        return 1

    mergeable = mergeable_entries(manifest)
    if not mergeable:
        print("ship package FAIL: nothing to ship (no mergeable:true entries)",
              file=out)
        return 1

    run_name = run_dir.name
    stamp = _stamp_fields(mergeable)
    verdict = stamp.get("verdict") or "TERMINAL_CONFIRMED"
    stamp_sha = stamp.get("sha256") or "?"
    n = len(mergeable)

    target_ref = resolve_ship_target(spec, target)
    try:
        head = resolve_target_head(spec.repo, target_ref, no_fetch=no_fetch)
    except RuntimeError as e:
        print(f"ship package ERROR: {e}", file=err)
        return 1

    # 2. Worktree at the gate-verified head.
    wt = Path(workdir) if workdir else default_package_workdir(spec, run_name)
    if wt.exists():
        print(
            f"ship package ERROR: workdir already exists: {wt} "
            f"(remove it or pass --workdir)",
            file=err)
        return 1
    try:
        wt.parent.mkdir(parents=True, exist_ok=True)
        vcs.worktree_add(spec.repo, wt, head)
        if (Path(spec.repo) / ".gitmodules").exists():
            timeout = int(getattr(spec, "timeout", None) or vcs.WORKTREE_TIMEOUT)
            vcs.submodule_update(wt, timeout=timeout)
    except RuntimeError as e:
        print(f"ship package ERROR: worktree setup failed: {e}", file=err)
        return 1

    br = (branch.strip() if branch and str(branch).strip()
          else default_package_branch(run_name))
    co = vcs.git(wt, "checkout", "-b", br)
    if co.returncode != 0:
        print(
            f"ship package ERROR: git checkout -b {br} failed: "
            f"{(co.stderr or co.stdout or '')[:300]}",
            file=err)
        return 1

    # 3. Apply certified set in acceptance order.
    from .reverify import load_entry_patch
    files_changed: list = []
    patch_texts: dict = {}
    for a in mergeable:
        order = a.get("order")
        try:
            patch = load_entry_patch(run_dir, a)
            # Capture patch text for unsafe disclosure before apply mutates tree.
            rel = a.get("patch_path")
            if rel:
                try:
                    patch_texts[order] = (run_dir / rel).read_text()
                except OSError:
                    patch_texts[order] = ""
            _apply_patch_to_work(patch, wt)
            for e in patch.edits:
                if e.path not in files_changed:
                    files_changed.append(e.path)
        except Exception as e:
            print(
                f"ship package FAIL: apply mismatch at order={order} "
                f"(integrity error — stamp said these bytes fit this head): {e}",
                file=out)
            return 1

    # 4. Single certified-set commit.
    subject = f"perf: ARO {n}-edit certified set ({verdict})"
    body = f"Campaign run: {run_name}\nstamp sha256: {stamp_sha}"
    add = vcs.git(wt, "add", "-A")
    if add.returncode != 0:
        print(
            f"ship package ERROR: git add failed: "
            f"{(add.stderr or add.stdout or '')[:300]}",
            file=err)
        return 1
    # Detect empty apply (should not happen if patches changed bytes).
    st = vcs.status_porcelain(wt)
    if not any(line.strip() and not line.startswith("??")
               and not line.startswith("!!") for line in st.splitlines()):
        # staged? status after add — check if anything to commit via diff --cached
        cached = vcs.git(wt, "diff", "--cached", "--quiet")
        if cached.returncode == 0:
            print(
                "ship package FAIL: nothing to commit after applying certified "
                "set (patches produced no file changes)",
                file=out)
            return 1
    cm = vcs.git(
        wt,
        "-c", "user.name=aro",
        "-c", "user.email=aro@example.invalid",
        "commit", "-qm", subject, "-m", body,
    )
    if cm.returncode != 0:
        print(
            f"ship package ERROR: git commit failed: "
            f"{(cm.stderr or cm.stdout or '')[:300]}",
            file=err)
        return 1

    # 5. PR body.
    try:
        body_md = generate_pr_body(
            spec, manifest,
            run_dir=run_dir, run_name=run_name, mergeable=mergeable,
            files_changed=files_changed, patch_texts=patch_texts,
        )
        body_path = run_dir / PR_BODY_NAME
        body_path.write_text(body_md)
    except OSError as e:
        print(f"ship package ERROR: failed to write pr_body.md: {e}", file=err)
        return 1

    print("ship package PASS — certified branch ready", file=out)
    print(f"  workdir:  {wt}", file=out)
    print(f"  branch:   {br}", file=out)
    print(f"  body:     {body_path}", file=out)
    print(f"  commit:   {subject}", file=out)
    print(f"  head:     {vcs.rev_parse(wt, 'HEAD')}", file=out)
    print("next:", file=out)
    print("  1. add supplementary tests (dual-green on baseline + this branch)",
          file=out)
    print("  2. optional: style: cargo fmt (idempotent)", file=out)
    print(f"  3. aro ship conformance <spec> --workdir {wt}", file=out)
    print(
        f"  4. aro ship open <spec> --manifest {run_dir} --workdir {wt}",
        file=out)
    return 0


# ---------------------------------------------------------------------------
# ship open — push + gh pr create under machine gates
# ---------------------------------------------------------------------------

# CompletedProcess-like for git push: .returncode, .stdout, .stderr
PushRunner = Callable[[Path, list], "subprocess.CompletedProcess"]


def default_push_runner(cwd: Path, argv: list) -> subprocess.CompletedProcess:
    """Invoke ``git -C <cwd> <argv>`` (network for push). Not for tests."""
    return subprocess.run(
        ["git", "-C", str(cwd), *argv],
        capture_output=True, text=True, timeout=PUSH_TIMEOUT_S,
    )


def _branch_name(workdir) -> Optional[str]:
    out = vcs.git(workdir, "rev-parse", "--abbrev-ref", "HEAD")
    if out.returncode != 0:
        return None
    name = (out.stdout or "").strip()
    if not name or name == "HEAD":
        return None
    return name


def _commits_after_base(workdir, base_ref: str) -> list:
    """Return ``[(sha, subject), ...]`` oldest-first for ``base_ref..HEAD``."""
    # Prefer the ship-target local name (branch without remote prefix) when present.
    out = vcs.git(
        workdir, "log", "--reverse", "--format=%H%x00%s", f"{base_ref}..HEAD")
    if out.returncode != 0:
        # Fallback: single-parent walk from first unique commit via merge-base.
        mb = vcs.git(workdir, "merge-base", base_ref, "HEAD")
        if mb.returncode != 0 or not (mb.stdout or "").strip():
            raise RuntimeError(
                f"cannot list commits after {base_ref}: "
                f"{(out.stderr or out.stdout or '')[:200]}")
        base_sha = mb.stdout.strip()
        out = vcs.git(
            workdir, "log", "--reverse", "--format=%H%x00%s",
            f"{base_sha}..HEAD")
        if out.returncode != 0:
            raise RuntimeError(
                f"cannot list commits after merge-base: "
                f"{(out.stderr or out.stdout or '')[:200]}")
    rows = []
    for line in (out.stdout or "").splitlines():
        if "\x00" not in line:
            continue
        sha, subj = line.split("\x00", 1)
        rows.append((sha.strip(), subj.strip()))
    return rows


def open_pr(spec, manifest_path, workdir, *,
            record=None,
            title: Optional[str] = None,
            target: Optional[str] = None,
            no_fetch: bool = False,
            gh_runner: Optional[GhRunner] = None,
            push_runner: Optional[PushRunner] = None,
            file=None) -> int:
    """Machine-gated push + ``gh pr create``. Exit 0 / 1 (fail-closed).

    *gh_runner* / *push_runner* are injectable seams (tests never hit network).
    """
    out = file if file is not None else sys.stdout
    err = sys.stderr
    workdir = Path(workdir)
    runner_gh = gh_runner if gh_runner is not None else default_gh_runner
    runner_push = push_runner if push_runner is not None else default_push_runner

    # 1. Re-gate — baseline still current at open time.
    gate_code = gate(
        spec, manifest_path, target=target, no_fetch=no_fetch, file=out)
    if gate_code != 0:
        print("ship open REFUSE: gate did not PASS", file=out)
        return 1

    if not workdir.is_dir():
        print(f"ship open REFUSE: workdir is not a directory: {workdir}",
              file=out)
        return 1

    head_sha = vcs.rev_parse(workdir, "HEAD")
    if not head_sha:
        print(
            f"ship open REFUSE: workdir is not a git checkout: {workdir}",
            file=out)
        return 1

    # 2. Conformance record exists, all_green, head_sha == HEAD.
    rec_path = (Path(record) if record
                else (workdir / DEFAULT_CONFORMANCE_OUT_NAME))
    if not rec_path.is_file():
        print(
            f"ship open REFUSE: missing conformance record at {rec_path}",
            file=out)
        return 1
    try:
        rec = json.loads(rec_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ship open REFUSE: cannot read conformance record: {e}",
              file=out)
        return 1
    if not rec.get("all_green"):
        print(
            "ship open REFUSE: conformance record all_green is not true",
            file=out)
        return 1
    rec_head = str(rec.get("head_sha") or "")
    if rec_head != head_sha:
        print(
            f"ship open REFUSE: conformance record head_sha ({rec_head[:12] or '?'}) "
            f"!= workdir HEAD ({head_sha[:12]}) — record is stale",
            file=out)
        return 1

    # 3. No uncommitted tracked changes.
    try:
        dirty = workdir_has_tracked_dirt(workdir)
    except RuntimeError as e:
        print(f"ship open REFUSE: git status failed: {e}", file=out)
        return 1
    if dirty:
        print(
            "ship open REFUSE: workdir has uncommitted tracked changes",
            file=out)
        return 1

    # 4. Branch identity + post-cert commit whitelist.
    branch = _branch_name(workdir)
    if not branch:
        print(
            "ship open REFUSE: workdir HEAD is detached "
            "(need a named branch to push)",
            file=out)
        return 1

    target_ref = resolve_ship_target(spec, target)
    try:
        _remote, target_branch = split_remote_branch(target_ref)
    except ValueError as e:
        print(f"ship open REFUSE: {e}", file=out)
        return 1

    # 5. Branch is not the ship-target branch itself.
    if branch == target_branch:
        print(
            f"ship open REFUSE: workdir branch {branch!r} is the ship-target "
            f"branch itself — package onto aro/ship-* first",
            file=out)
        return 1

    # Commits unique to this branch vs the ship-target ref (local name first).
    base_candidates = [target_ref, target_branch, f"origin/{target_branch}"]
    commits = None
    last_err = None
    for base in base_candidates:
        try:
            commits = _commits_after_base(workdir, base)
            if commits is not None:
                break
        except RuntimeError as e:
            last_err = e
            continue
    if commits is None:
        print(
            f"ship open REFUSE: cannot resolve commits after ship target "
            f"({last_err})",
            file=out)
        return 1
    if not commits:
        print(
            "ship open REFUSE: no commits on branch after ship-target base "
            "(empty branch?)",
            file=out)
        return 1

    cert_sha, cert_subj = commits[0]
    if not _CERTIFIED_SUBJECT.match(cert_subj):
        print(
            f"ship open REFUSE: first branch commit is not the certified-set "
            f"commit: {cert_sha[:12]} {cert_subj!r}",
            file=out)
        return 1
    for sha, subj in commits[1:]:
        if not _POST_CERT_SUBJECT.match(subj):
            print(
                f"ship open REFUSE: non-whitelisted post-cert commit "
                f"{sha[:12]}: {subj!r} "
                f"(allowed: ^test(|:)… or 'style: cargo fmt')",
                file=out)
            return 1

    # Resolve run dir + body file.
    try:
        run_dir, _man = resolve_run_and_manifest(manifest_path)
    except FileNotFoundError as e:
        print(f"ship open REFUSE: {e}", file=out)
        return 1
    body_path = run_dir / PR_BODY_NAME
    if not body_path.is_file():
        print(
            f"ship open REFUSE: missing PR body at {body_path} "
            f"(run ship package first)",
            file=out)
        return 1

    if not title or not str(title).strip():
        title = cert_subj
    else:
        title = str(title).strip()

    remote = resolve_ship_remote(spec)
    try:
        labels = resolve_pr_labels(spec)
    except ValueError as e:
        print(f"ship open REFUSE: {e}", file=out)
        return 1

    # Push.
    push_cp = runner_push(workdir, ["push", "-u", remote, branch])
    if getattr(push_cp, "returncode", 1) != 0:
        err_t = (getattr(push_cp, "stderr", None)
                 or getattr(push_cp, "stdout", None) or "").strip()
        print(
            f"ship open ERROR: git push failed "
            f"(rc={getattr(push_cp, 'returncode', '?')}): "
            f"{err_t[:400] or 'no output'}",
            file=err)
        return 1

    # gh pr create
    gh_argv = [
        "pr", "create",
        "--title", title,
        "--body-file", str(body_path),
        "--base", target_branch,
        "--head", branch,
    ]
    for lab in labels:
        gh_argv.extend(["--label", lab])
    gh_cp = runner_gh(gh_argv)
    if getattr(gh_cp, "returncode", 1) != 0:
        err_t = (getattr(gh_cp, "stderr", None)
                 or getattr(gh_cp, "stdout", None) or "").strip()
        print(
            f"ship open ERROR: gh pr create failed "
            f"(rc={getattr(gh_cp, 'returncode', '?')}): "
            f"{err_t[:400] or 'no output'}",
            file=err)
        return 1

    pr_url = (getattr(gh_cp, "stdout", None) or "").strip().splitlines()
    pr_url = pr_url[-1].strip() if pr_url else ""
    print("ship open PASS — PR opened", file=out)
    print(f"  branch:  {branch}", file=out)
    print(f"  base:    {target_branch}", file=out)
    print(f"  remote:  {remote}", file=out)
    if labels:
        print(f"  labels:  {', '.join(labels)}", file=out)
    print(f"  body:    {body_path}", file=out)
    if pr_url:
        print(f"  url:     {pr_url}", file=out)
    return 0


def watch(spec, manifest_path, pr: str, *,
          gh_runner: Optional[GhRunner] = None,
          file=None) -> int:
    """One-shot PR outcome poll. Returns process exit code (0 ok / 1 error).

    *gh_runner*: injectable ``(argv: list) -> CompletedProcess``; default shells
    out to ``gh``. Tests pass a fake so no network is touched.
    *spec* is accepted for CLI symmetry (repo context) but watch keys off
    *manifest_path* + *pr* only.
    """
    del spec  # reserved; poll is pr + manifest driven
    out = file if file is not None else sys.stdout
    err = sys.stderr
    runner = gh_runner if gh_runner is not None else default_gh_runner
    pr_ref = str(pr).strip()
    if not pr_ref:
        print("ship watch ERROR: --pr is empty", file=err)
        return 1

    try:
        run_dir, man_path = resolve_run_and_manifest(manifest_path)
        manifest = json.loads(man_path.read_text())
    except (OSError, json.JSONDecodeError, FileNotFoundError) as e:
        print(f"ship watch ERROR: failed to load manifest: {e}", file=err)
        return 1

    try:
        payload = fetch_pr_payload(pr_ref, gh_runner=runner)
    except RuntimeError as e:
        print(f"ship watch ERROR: {e}", file=err)
        return 1

    verdict = classify_pr_verdict(payload)
    pr_url = str(payload.get("url") or pr_ref)
    pr_key = pr_feedback_key(pr_ref if pr_ref.isdigit() or "/" in pr_ref
                             else pr_url)
    # Prefer numeric key from URL when pr_ref was a bare number or URL.
    pr_key = pr_feedback_key(pr_url) if payload.get("url") else pr_key

    if verdict == "merged":
        merge_sha = _merge_sha_from_payload(payload)
        n = stamp_shipped(manifest, pr=pr_url, merge_sha=merge_sha)
        try:
            write_manifest(man_path, manifest)
        except OSError as e:
            print(f"ship watch ERROR: failed to write manifest: {e}", file=err)
            return 1
        print("ship watch MERGED — stamped mergeable entries", file=out)
        print(f"  pr:         {pr_url}", file=out)
        print(f"  merge_sha:  {merge_sha or '?'}", file=out)
        print(f"  stamped:    {n} mergeable entr"
              f"{'y' if n == 1 else 'ies'}", file=out)
        print(f"  manifest:   {man_path}", file=out)
        return 0

    if verdict == "open":
        print("ship watch OPEN — no actionable feedback; no-op", file=out)
        print(f"  pr:              {pr_url}", file=out)
        print(f"  reviewDecision:  {payload.get('reviewDecision') or '(none)'}",
              file=out)
        return 0

    # closed (unmerged) or changes_requested — harvest + queue
    items = harvest_feedback_items(payload, manifest)
    feedback_doc = {
        "pr": pr_url,
        "pr_key": pr_key,
        "state": payload.get("state"),
        "reviewDecision": payload.get("reviewDecision"),
        "verdict": verdict,
        "headRefOid": payload.get("headRefOid"),
        "harvested_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }
    try:
        fb_path = write_feedback_file(run_dir, pr_key, feedback_doc)
    except OSError as e:
        print(f"ship watch ERROR: failed to write feedback: {e}", file=err)
        return 1

    seeds = reattempt_seeds_from_items(items, pr=pr_url)
    try:
        q_path, n_added, n_dup = append_reattempt_queue(run_dir, seeds)
    except OSError as e:
        print(f"ship watch ERROR: failed to write reattempt queue: {e}",
              file=err)
        return 1

    n_bound = sum(1 for it in items if it.get("entry") is not None)
    n_unbound = len(items) - n_bound

    if verdict == "closed":
        print("ship watch CLOSED — harvested feedback (unmerged)", file=out)
    else:
        print(
            "ship watch CHANGES_REQUESTED — harvested feedback; "
            "PR stays open awaiting re-certified revision",
            file=out)
    print(f"  pr:         {pr_url}", file=out)
    print(f"  feedback:   {fb_path} ({len(items)} items; "
          f"{n_bound} bound, {n_unbound} unbound)", file=out)
    print(f"  queue:      {q_path} (+{n_added} seed(s), "
          f"{n_dup} dup skipped)", file=out)
    if verdict == "closed":
        print("  manifest:   untouched", file=out)
    else:
        print("  manifest:   untouched (PR still open)", file=out)
    return 0


def cli(args) -> None:
    """`aro ship gate|package|conformance|open|watch …`."""
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

    if action == "package":
        code = package(
            sp, args.manifest,
            target=getattr(args, "target", None),
            no_fetch=bool(getattr(args, "no_fetch", False)),
            branch=getattr(args, "branch", None),
            workdir=getattr(args, "workdir", None),
        )
        raise SystemExit(code)

    if action == "conformance":
        code = conformance(
            sp, args.workdir,
            out_path=getattr(args, "out", None),
            spec_path=args.spec,
        )
        raise SystemExit(code)

    if action == "open":
        code = open_pr(
            sp, args.manifest, args.workdir,
            record=getattr(args, "record", None),
            title=getattr(args, "title", None),
            target=getattr(args, "target", None),
            no_fetch=bool(getattr(args, "no_fetch", False)),
        )
        raise SystemExit(code)

    if action == "watch":
        code = watch(
            sp, args.manifest, args.pr,
        )
        raise SystemExit(code)

    raise SystemExit(f"unknown ship action {action!r}")
