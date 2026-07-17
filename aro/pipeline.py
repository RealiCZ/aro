"""`aro pipeline` — campaign → certify → ship → opened PR, checkpointed.

Sequences existing modules without reimplementing their logic. Durable stage
state lives at ``<out_dir>/pipeline-state.json`` so a plain re-run (or
``--continue``) resumes from the first incomplete stage.

Stage chain:

  [stage 0 bootstrap — only when ``--manifest`` is omitted]
    settle ledger → re-pin baseline → seed → auto-name out-dir
  sweep → certify → gate → package ──(exit 2: supplement work order)──►
  [operator dual-green] → conformance → open ──(exit 0: PR URL)

With ``--manifest`` the T44 path is unchanged (no bootstrap).

Exit codes: 0 = PR opened · 2 = designed stop (work order + resume command) ·
1 = error.

Injectable stage callables keep tests hermetic; production defaults are thin
adapters over ``sweep`` / ``certify`` / ``ship``.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# --- constants ----------------------------------------------------------------

STATE_NAME = "pipeline-state.json"
SEEDS_NAME = "seeds.json"
DEFAULT_RUNS_ROOT = ".aro-runs"

# stage 0 is bootstrap (no --manifest only); remaining stages match T44.
STAGES = ("sweep", "certify", "gate", "package", "conformance", "open")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WORK_ORDER = 2

_PR_DISCIPLINE_ONELINER = (
    "pr-discipline: dual-green on baseline + branch; whitelist commits only "
    "(test:… / style: cargo fmt); fmt must be idempotent"
)

# Targeted rewrite of baseline_ref: match the value after "baseline_ref":
_BASELINE_REF_RE = re.compile(
    r'("baseline_ref"\s*:\s*")([^"]*)(")'
)


# --- state --------------------------------------------------------------------

def state_path(out_dir) -> Path:
    return Path(out_dir) / STATE_NAME


def empty_state() -> dict:
    return {
        "stages": {},
        "updated": _now_iso(),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(out_dir) -> dict:
    path = state_path(out_dir)
    if not path.is_file():
        return empty_state()
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return empty_state()
    if not isinstance(doc, dict):
        return empty_state()
    stages = doc.get("stages")
    if not isinstance(stages, dict):
        stages = {}
    out = {"stages": stages, "updated": doc.get("updated") or _now_iso()}
    if "bootstrap" in doc:
        out["bootstrap"] = doc["bootstrap"]
    return out


def save_state(out_dir, state: dict) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated"] = _now_iso()
    path = state_path(out)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def is_stage_done(stages: dict, name: str) -> bool:
    """True when the stage is checked off (done or skipped for sweep)."""
    val = stages.get(name)
    if name == "sweep":
        return val in ("done", "skipped")
    if name in ("package", "open"):
        if isinstance(val, dict):
            return bool(val.get("done"))
        return False
    return val == "done"


def mark_done(state: dict, name: str, value: Any = "done") -> None:
    state.setdefault("stages", {})[name] = value


# --- stage result helpers -----------------------------------------------------

def _exit_code(result) -> int:
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        if "exit_code" in result:
            return int(result["exit_code"])
        if "code" in result:
            return int(result["code"])
        return 0
    code = getattr(result, "exit_code", None)
    if code is not None:
        return int(code)
    return 0


def _meta(result) -> dict:
    if isinstance(result, dict):
        return result
    if result is None or isinstance(result, int):
        return {}
    out = {}
    for key in ("workdir", "branch", "files_changed", "url", "message"):
        if hasattr(result, key):
            out[key] = getattr(result, key)
    return out


# --- bootstrap helpers --------------------------------------------------------

def auto_out_dir(runs_root, spec_name: str, *,
                 today: Optional[str] = None) -> Path:
    """``<runs_root>/<spec.name>-auto-<YYYYMMDD>`` with ``-2``, ``-3`` on collision."""
    root = Path(runs_root)
    day = today or datetime.now(timezone.utc).strftime("%Y%m%d")
    base = f"{spec_name}-auto-{day}"
    candidate = root / base
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = root / f"{base}-{n}"
        if not candidate.exists():
            return candidate
        n += 1


def repin_baseline_ref(spec_path, new_sha: str, *,
                       load_fn: Optional[Callable] = None) -> dict:
    """Rewrite ONLY the ``baseline_ref`` value in the spec file.

    Targeted textual replacement preserves surrounding formatting. Validates by
    re-loading the spec and comparing the resolved ``baseline_ref``. Returns
    ``{old, new, changed: bool}``.
    """
    path = Path(spec_path)
    text = path.read_text()
    m = _BASELINE_REF_RE.search(text)
    if not m:
        raise RuntimeError(
            f"bootstrap re-pin: no baseline_ref string value found in {path}")
    old = m.group(2)
    if old == new_sha:
        return {"old": old, "new": new_sha, "changed": False}

    new_text, n = _BASELINE_REF_RE.subn(
        lambda mm: mm.group(1) + new_sha + mm.group(3),
        text,
        count=1,
    )
    if n != 1:
        raise RuntimeError(
            f"bootstrap re-pin: expected exactly 1 replacement, got {n}")
    # Byte-level guarantee: only the baseline_ref value changed.
    # (Other content must be identical.)
    path.write_text(new_text)

    # Validate by reload.
    if load_fn is None:
        from . import spec as specmod
        load_fn = specmod.load
    try:
        reloaded = load_fn(path)
    except Exception as e:
        # Best-effort restore.
        path.write_text(text)
        raise RuntimeError(
            f"bootstrap re-pin: reload after rewrite failed: {e}") from e
    resolved = getattr(reloaded, "baseline_ref", None)
    if str(resolved) != str(new_sha):
        path.write_text(text)
        raise RuntimeError(
            f"bootstrap re-pin: reload baseline_ref={resolved!r} "
            f"!= expected {new_sha!r}; restored original file")
    return {"old": old, "new": new_sha, "changed": True}


def write_seeds_file(out_dir, seeds: list) -> Path:
    """Write ``seeds.json`` under ``out_dir``; return the path."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / SEEDS_NAME
    path.write_text(json.dumps(seeds, indent=2, ensure_ascii=False) + "\n")
    return path


def bootstrap(spec, *,
              spec_path: str,
              runs_root,
              skip_ledger: bool = False,
              settle_fn: Optional[Callable] = None,
              resolve_head_fn: Optional[Callable] = None,
              repin_fn: Optional[Callable] = None,
              collect_seeds_fn: Optional[Callable] = None,
              today: Optional[str] = None,
              file=None) -> dict:
    """Stage 0: settle ledger → re-pin baseline → seed → auto-name out-dir.

    Returns a result dict:

      {
        "exit_code": 0|1,
        "out_dir": Path|None,
        "bootstrap": {ledger_settled, repin, seeds, out_dir},
      }

    Fail-closed: any gh/network failure during settle exits 1 before re-pin
    (unless ``skip_ledger``).
    """
    from . import ship as shipmod

    out = file if file is not None else sys.stdout
    runs_root = Path(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)

    ledger_settled: Any = False
    if skip_ledger:
        print(
            "pipeline bootstrap: settle ledger SKIPPED (--skip-ledger)",
            file=out,
        )
        ledger_settled = "skipped"
    else:
        print("pipeline bootstrap: settle ledger …", file=out)
        settle = settle_fn or (
            lambda sp, root, **kw: shipmod.watch_all(sp, root, file=out, **kw)
        )
        try:
            code = settle(spec, runs_root)
        except Exception as e:
            print(f"pipeline bootstrap ERROR: settle ledger failed: {e}",
                  file=out)
            return {"exit_code": EXIT_ERROR, "out_dir": None, "bootstrap": None}
        if _exit_code(code) != 0:
            print(
                "pipeline bootstrap ERROR: settle ledger failed "
                "(gh/network); refusing to continue without --skip-ledger",
                file=out,
            )
            return {"exit_code": EXIT_ERROR, "out_dir": None, "bootstrap": None}
        ledger_settled = True
        print("pipeline bootstrap: ledger settled", file=out)

    # Re-pin baseline to ship-target head.
    print("pipeline bootstrap: re-pin baseline …", file=out)
    target_ref = shipmod.resolve_ship_target(spec)
    if resolve_head_fn is not None:
        try:
            head = resolve_head_fn(spec, target_ref)
        except Exception as e:
            print(f"pipeline bootstrap ERROR: resolve ship-target head: {e}",
                  file=out)
            return {"exit_code": EXIT_ERROR, "out_dir": None, "bootstrap": None}
    else:
        try:
            head = shipmod.resolve_target_head(spec.repo, target_ref)
        except RuntimeError as e:
            print(f"pipeline bootstrap ERROR: {e}", file=out)
            return {"exit_code": EXIT_ERROR, "out_dir": None, "bootstrap": None}

    old_ref = str(getattr(spec, "baseline_ref", "") or "")
    repin_info: Any = None
    if old_ref == head:
        print(f"re-pin: already current ({head})", file=out)
        repin_info = None
    else:
        do_repin = repin_fn or repin_baseline_ref
        try:
            result = do_repin(spec_path, head)
        except Exception as e:
            print(f"pipeline bootstrap ERROR: re-pin failed: {e}", file=out)
            return {"exit_code": EXIT_ERROR, "out_dir": None, "bootstrap": None}
        if isinstance(result, dict) and result.get("changed"):
            print(f"re-pin: {result['old']} → {result['new']}", file=out)
            repin_info = {"old": result["old"], "new": result["new"]}
            # Keep in-memory spec consistent for the rest of this process.
            try:
                spec.baseline_ref = head
            except Exception:
                pass
        else:
            print(f"re-pin: already current ({head})", file=out)
            repin_info = None

    # Collect seeds from ledger runs' reattempt queues.
    if collect_seeds_fn is not None:
        seeds = list(collect_seeds_fn(spec, runs_root) or [])
    else:
        seeds = shipmod.collect_pending_seeds_from_ledger(spec, runs_root)

    # Auto-name out-dir.
    name = getattr(spec, "name", None) or "campaign"
    out_dir = auto_out_dir(runs_root, name, today=today)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_seeds_file(out_dir, seeds)
    print(
        f"pipeline bootstrap: out_dir={out_dir} seeds={len(seeds)}",
        file=out,
    )

    boot = {
        "ledger_settled": ledger_settled,
        "repin": repin_info,
        "seeds": len(seeds),
        "out_dir": str(out_dir),
    }
    return {
        "exit_code": EXIT_OK,
        "out_dir": out_dir,
        "bootstrap": boot,
        "seeds_path": str(out_dir / SEEDS_NAME),
    }


# --- production adapters (thin; no logic reimplementation) --------------------

def default_sweep_fn(spec, out_dir, *, spec_path: Optional[str] = None,
                     seeds: Optional[str] = None, **_kw) -> int:
    """Invoke sweep ``--attempt`` into ``out_dir`` via the existing CLI path."""
    from types import SimpleNamespace

    from . import sweep as sweepmod

    path = spec_path
    if not path:
        # Best-effort: callers should pass the original CLI path.
        raise RuntimeError(
            "pipeline sweep adapter requires spec_path (the target JSON path)")
    args = SimpleNamespace(
        spec=str(path),
        out=None,
        min_pct=1.5,
        top=40,
        attempt=True,
        diverge=False,
        critic=False,
        max_attempts=None,
        rounds_per_fn=None,
        max_tries_per_fn=0,
        dry_rounds=None,
        fanout=None,
        gen_concurrency=8,
        out_dir=str(out_dir),
        prescreen=None,
        exhaustive=None,
        probe_factory=None,
        workloads=0,
        allow_stale_baseline=False,
        seeds=seeds,
    )
    del spec  # reloaded inside sweep.cli from path
    try:
        sweepmod.cli(args)
        return 0
    except SystemExit as e:
        code = e.code
        if code is None or code == 0:
            return 0
        if isinstance(code, int):
            return code
        return EXIT_ERROR


def default_certify_fn(spec, out_dir, **_kw):
    from . import certify as certmod

    return certmod.certify(spec, out_dir)


def default_gate_fn(spec, manifest_path, **_kw) -> int:
    from . import ship as shipmod

    return shipmod.gate(spec, manifest_path)


def default_package_fn(spec, manifest_path, *, workdir=None, branch=None,
                       **_kw) -> dict:
    """Call ``ship.package``; return exit_code + workdir/branch/files_changed."""
    from . import ship as shipmod

    run_dir, _man = shipmod.resolve_run_and_manifest(manifest_path)
    run_name = run_dir.name
    wt = Path(workdir) if workdir else shipmod.default_package_workdir(
        spec, run_name)
    br = (branch.strip() if branch and str(branch).strip()
          else shipmod.default_package_branch(run_name))
    code = shipmod.package(
        spec, manifest_path, workdir=str(wt), branch=br)
    files_changed: list = []
    if code == 0:
        files_changed = _mergeable_files(run_dir)
    return {
        "exit_code": code,
        "workdir": str(wt),
        "branch": br,
        "files_changed": files_changed,
    }


def _mergeable_files(run_dir: Path) -> list:
    """Paths touched by mergeable patches (coverage targets for dual-green)."""
    man_path = Path(run_dir) / "manifest.json"
    if not man_path.is_file():
        return []
    try:
        man = json.loads(man_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    files: list = []
    for a in man.get("accepted") or []:
        if not a.get("mergeable"):
            continue
        for f in a.get("files") or []:
            if f not in files:
                files.append(f)
    for f in man.get("files_touched") or []:
        if f not in files:
            files.append(f)
    return files


def default_conformance_fn(spec, workdir, *, spec_path: Optional[str] = None,
                           **_kw) -> int:
    from . import ship as shipmod

    return shipmod.conformance(spec, workdir, spec_path=spec_path)


def default_open_fn(spec, manifest_path, workdir, **_kw) -> dict:
    """Call ``ship.open_pr``; capture printed URL while streaming output."""
    from . import ship as shipmod

    class _Tee:
        def __init__(self, primary):
            self.primary = primary
            self.chunks: list[str] = []

        def write(self, s):
            self.primary.write(s)
            self.chunks.append(s if isinstance(s, str) else str(s))
            return len(s) if s is not None else 0

        def flush(self):
            if hasattr(self.primary, "flush"):
                self.primary.flush()

    tee = _Tee(sys.stdout)
    code = shipmod.open_pr(spec, manifest_path, workdir, file=tee)
    url = ""
    text = "".join(tee.chunks)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("url:"):
            url = stripped.split("url:", 1)[1].strip()
    return {"exit_code": code, "url": url}


# --- work-order printers ------------------------------------------------------

def _resume_cmd(spec_path: str, out_dir: Path) -> str:
    return f"aro pipeline {spec_path} --manifest {out_dir} --continue"


def _print_supplement_work_order(
        *, spec_path: str, out_dir: Path, workdir, branch,
        files_changed, file=None) -> None:
    out = file if file is not None else sys.stdout
    print(
        "pipeline STOP (work order): package complete — add dual-green "
        "supplements, then resume",
        file=out,
    )
    print(f"  workdir:  {workdir}", file=out)
    print(f"  branch:   {branch}", file=out)
    print(
        "Touched paths (coverage targets for dual-green tests):",
        file=out,
    )
    if files_changed:
        for p in files_changed:
            print(f"  - {p}", file=out)
    else:
        print("  (none recorded — inspect the packaged branch)", file=out)
    print(_PR_DISCIPLINE_ONELINER, file=out)
    print(f"Resume:\n  {_resume_cmd(spec_path, out_dir)}", file=out)


def _print_stop(*, kind: str, detail: str = "", resume: str = "",
                file=None) -> None:
    out = file if file is not None else sys.stdout
    print(f"pipeline STOP (work order): {kind}", file=out)
    if detail:
        print(detail.rstrip(), file=out)
    if resume:
        print(f"Resume:\n  {resume}", file=out)


# --- orchestrator -------------------------------------------------------------

def pipeline(spec, out_dir, *,
             continue_: bool = False,
             fresh: bool = False,
             no_sweep: bool = False,
             workdir=None,
             branch: Optional[str] = None,
             spec_path: Optional[str] = None,
             seeds: Optional[str] = None,
             sweep_fn: Optional[Callable] = None,
             certify_fn: Optional[Callable] = None,
             gate_fn: Optional[Callable] = None,
             package_fn: Optional[Callable] = None,
             conformance_fn: Optional[Callable] = None,
             open_fn: Optional[Callable] = None,
             file=None) -> int:
    """Run the checkpointed stage chain. Returns process exit code.

    ``continue_`` is accepted for CLI symmetry; resume is the default when
    state already exists (plain re-run == ``--continue``).

    When called after bootstrap, pass ``seeds`` (path to seeds.json) so the
    sweep adapter can bias frontier order. With ``--manifest`` (T44 path)
    bootstrap is absent and ``seeds`` is typically None.
    """
    del continue_  # same as plain re-run; kept for UX clarity at the CLI
    out = file if file is not None else sys.stdout
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spath = str(spec_path) if spec_path is not None else getattr(
        spec, "name", "<spec>")

    # Default seeds path if bootstrap wrote one and caller didn't override.
    if seeds is None:
        cand = out_dir / SEEDS_NAME
        if cand.is_file():
            seeds = str(cand)

    if fresh:
        sp = state_path(out_dir)
        if sp.is_file():
            sp.unlink()
            print(f"pipeline: --fresh cleared {sp}", file=out)

    state = load_state(out_dir)
    stages = state.setdefault("stages", {})

    sweep_fn = sweep_fn or default_sweep_fn
    certify_fn = certify_fn or default_certify_fn
    gate_fn = gate_fn or default_gate_fn
    package_fn = package_fn or default_package_fn
    conformance_fn = conformance_fn or default_conformance_fn
    open_fn = open_fn or default_open_fn

    resume = _resume_cmd(spath, out_dir)

    # ----- sweep --------------------------------------------------------------
    if is_stage_done(stages, "sweep"):
        mark = stages.get("sweep")
        print(f"pipeline: skip sweep ({mark})", file=out)
    elif no_sweep:
        print("pipeline: stage sweep … skipped (--no-sweep)", file=out)
        mark_done(state, "sweep", "skipped")
        save_state(out_dir, state)
    else:
        print("pipeline: stage sweep …", file=out)
        try:
            result = sweep_fn(
                spec, out_dir, spec_path=spath, seeds=seeds)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (
                0 if e.code is None else EXIT_ERROR)
            if code != 0:
                return EXIT_ERROR
            result = 0
        code = _exit_code(result)
        if code != 0:
            return EXIT_ERROR
        mark_done(state, "sweep", "done")
        save_state(out_dir, state)

    # ----- certify ------------------------------------------------------------
    if is_stage_done(stages, "certify"):
        print("pipeline: skip certify (done)", file=out)
    else:
        print("pipeline: stage certify …", file=out)
        result = certify_fn(spec, out_dir)
        code = _exit_code(result)
        if code == EXIT_WORK_ORDER:
            msg = ""
            meta = _meta(result)
            if meta.get("message"):
                msg = str(meta["message"])
            elif hasattr(result, "message") and result.message:
                msg = str(result.message)
            _print_stop(
                kind="certify work order",
                detail=msg,
                resume=resume,
                file=out,
            )
            return EXIT_WORK_ORDER
        if code != 0:
            return EXIT_ERROR
        mark_done(state, "certify", "done")
        save_state(out_dir, state)

    # ----- gate ---------------------------------------------------------------
    if is_stage_done(stages, "gate"):
        print("pipeline: skip gate (done)", file=out)
    else:
        print("pipeline: stage gate …", file=out)
        result = gate_fn(spec, out_dir)
        code = _exit_code(result)
        if code != 0:
            _print_stop(
                kind="gate FAIL — re-certification required "
                     "(do NOT hand-rebase certified edits)",
                detail=(
                    "  1. update the spec's baseline_ref to the new head sha\n"
                    "  2. aro recheck candidates --spec <spec> --out <run>\n"
                    "  3. aro certify <spec> --manifest <run>  "
                    "(or pipeline --continue after re-cert)\n"
                    "  4. re-enter: aro pipeline <spec> --manifest <run> "
                    "--continue"
                ),
                resume=resume,
                file=out,
            )
            return EXIT_WORK_ORDER
        mark_done(state, "gate", "done")
        save_state(out_dir, state)

    # ----- package ------------------------------------------------------------
    if is_stage_done(stages, "package"):
        print("pipeline: skip package (done)", file=out)
    else:
        print("pipeline: stage package …", file=out)
        result = package_fn(
            spec, out_dir, workdir=workdir, branch=branch)
        code = _exit_code(result)
        if code != 0:
            return EXIT_ERROR
        meta = _meta(result)
        pkg_workdir = meta.get("workdir") or workdir
        pkg_branch = meta.get("branch") or branch or ""
        files_changed = list(meta.get("files_changed") or [])
        if not pkg_workdir:
            return EXIT_ERROR
        mark_done(state, "package", {
            "done": True,
            "workdir": str(pkg_workdir),
            "branch": str(pkg_branch),
        })
        save_state(out_dir, state)
        # Designed mid-chain stop: operator adds dual-green supplements.
        _print_supplement_work_order(
            spec_path=spath,
            out_dir=out_dir,
            workdir=pkg_workdir,
            branch=pkg_branch,
            files_changed=files_changed,
            file=out,
        )
        return EXIT_WORK_ORDER

    # Package done — need workdir for remaining stages.
    pkg = stages.get("package") or {}
    if not isinstance(pkg, dict) or not pkg.get("workdir"):
        print(
            "pipeline ERROR: package stage marked done but workdir missing "
            "in state",
            file=out,
        )
        return EXIT_ERROR
    pkg_workdir = Path(pkg["workdir"])

    # ----- conformance --------------------------------------------------------
    if is_stage_done(stages, "conformance"):
        print("pipeline: skip conformance (done)", file=out)
    else:
        print("pipeline: stage conformance …", file=out)
        result = conformance_fn(
            spec, pkg_workdir, spec_path=spath)
        code = _exit_code(result)
        if code != 0:
            _print_stop(
                kind="conformance FAIL — fix tests / checks, then resume",
                detail=(
                    "  inspect the per-check table printed by ship "
                    "conformance above;\n"
                    "  allowed post-cert commits: test:… / style: cargo fmt"
                ),
                resume=resume,
                file=out,
            )
            return EXIT_WORK_ORDER
        mark_done(state, "conformance", "done")
        save_state(out_dir, state)

    # ----- open ---------------------------------------------------------------
    if is_stage_done(stages, "open"):
        print("pipeline: skip open (done)", file=out)
        open_meta = stages.get("open") or {}
        url = open_meta.get("url") if isinstance(open_meta, dict) else ""
        if url:
            print(f"pipeline: PR already opened: {url}", file=out)
        return EXIT_OK

    print("pipeline: stage open …", file=out)
    result = open_fn(spec, out_dir, pkg_workdir)
    code = _exit_code(result)
    if code != 0:
        _print_stop(
            kind="open REFUSE — resolve the printed reason, then resume",
            detail="",
            resume=resume,
            file=out,
        )
        return EXIT_WORK_ORDER
    meta = _meta(result)
    url = str(meta.get("url") or "")
    mark_done(state, "open", {"done": True, "url": url})
    save_state(out_dir, state)
    if url:
        print(f"pipeline: PR opened: {url}", file=out)
    else:
        print("pipeline: PR opened (no URL captured)", file=out)
    return EXIT_OK


# --- CLI ----------------------------------------------------------------------

def cli(args) -> None:
    """``aro pipeline <spec> [--manifest DIR] [--continue] [--fresh]
    [--no-sweep] [--workdir DIR] [--runs-root DIR] [--skip-ledger]``.

    Without ``--manifest``: stage-0 bootstrap (settle / re-pin / seed /
    auto-name out-dir) then the T44 chain. With ``--manifest``: T44 path only
    (no bootstrap).
    """
    from . import spec as specmod

    sp = specmod.load(args.spec)
    manifest = getattr(args, "manifest", None)
    skip_ledger = bool(getattr(args, "skip_ledger", False))
    runs_root = getattr(args, "runs_root", None) or DEFAULT_RUNS_ROOT

    if not manifest:
        # Stage 0 bootstrap → auto out-dir, then continue into the chain.
        boot = bootstrap(
            sp,
            spec_path=args.spec,
            runs_root=runs_root,
            skip_ledger=skip_ledger,
        )
        if boot["exit_code"] != 0:
            raise SystemExit(boot["exit_code"])
        out_dir = Path(boot["out_dir"])
        # Record bootstrap into the new run's pipeline-state.json before stages.
        state = empty_state()
        state["bootstrap"] = boot["bootstrap"]
        save_state(out_dir, state)
        # Reload spec if re-pin rewrote the file.
        if boot["bootstrap"] and boot["bootstrap"].get("repin"):
            sp = specmod.load(args.spec)
        code = pipeline(
            sp, out_dir,
            continue_=bool(getattr(args, "pipeline_continue", False)),
            fresh=bool(getattr(args, "fresh", False)),
            no_sweep=bool(getattr(args, "no_sweep", False)),
            workdir=getattr(args, "workdir", None),
            branch=getattr(args, "branch", None),
            spec_path=args.spec,
            seeds=boot.get("seeds_path"),
        )
        raise SystemExit(code)

    out_dir = Path(manifest)
    if out_dir.is_file():
        out_dir = out_dir.parent
    # Allow creating a new run dir for a full pipeline (sweep will populate it).
    out_dir.mkdir(parents=True, exist_ok=True)

    code = pipeline(
        sp, out_dir,
        continue_=bool(getattr(args, "pipeline_continue", False)),
        fresh=bool(getattr(args, "fresh", False)),
        no_sweep=bool(getattr(args, "no_sweep", False)),
        workdir=getattr(args, "workdir", None),
        branch=getattr(args, "branch", None),
        spec_path=args.spec,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    from .cli import main as _cli_main
    _cli_main(["pipeline"] + sys.argv[1:])
