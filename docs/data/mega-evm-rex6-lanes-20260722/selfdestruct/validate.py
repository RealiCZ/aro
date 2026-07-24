#!/usr/bin/env python3
"""Reproduce Lane 3 BENCH/DIFF through ARO's injected-example APIs.

Runs are serialized by an ARO-local advisory lock. Evidence is assembled in a
unique staging directory and published atomically only after validation and
verified disposable-worktree/Cargo-target cleanup both succeed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ARO_ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = Path(__file__).resolve().parent
SOURCE_REPO = Path(os.environ.get("MEGA_EVM_ARO_REPO", "/home/mega-engineer/workspace/mega-evm-aro"))
WORKTREE_PARENT = Path(os.environ.get("LANE3_WORKTREE_PARENT", "/nvme2/mega-engineer/workspace"))
BASELINE = "245476834741de1e1a615d22e6287621b64f30cb"
TIMED_PROBE = "probes/mega_evm_rex6_selfdestruct.rs"
DIFF_PROBE = "probes/mega_evm_rex6_selfdestruct_diff.rs"
PKG = "mega-evm"
BENCH_EXAMPLE = "aro_rex6_lane3_selfdestruct"
DIFF_EXAMPLE = "aro_rex6_lane3_selfdestruct_diff"
FINAL_OUTPUTS = (
    "bench.stdout",
    "diff-run1.stdout",
    "diff-run2.stdout",
    "validation.json",
    "SHA256SUMS",
)

sys.path.insert(0, str(ARO_ROOT))
from aro.spec import Goal, Stop, TargetSpec  # noqa: E402
from aro.target import SpecTarget  # noqa: E402


def run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    if path.exists():
        raise RuntimeError(f"directory survived removal: {path}")


def cleanup_validation_run(source_repo: Path, worktree: Path, target_dir: Path) -> None:
    """Best-effort every cleanup step, then fail if any step or invariant failed."""
    errors: list[str] = []

    try:
        registrations = run(["git", "-C", str(source_repo), "worktree", "list", "--porcelain"])
        if str(worktree) in registrations:
            run([
                "git", "-C", str(source_repo), "worktree", "remove", "--force", str(worktree)
            ])
    except Exception as exc:
        errors.append(f"force-remove registered worktree failed: {exc}")

    try:
        remove_tree(worktree)
    except Exception as exc:
        errors.append(f"residual worktree path removal failed: {exc}")

    # Prune after residual-path removal so a failed worktree-remove cannot leave a
    # stale registration merely because its directory existed during an earlier prune.
    try:
        run(["git", "-C", str(source_repo), "worktree", "prune"])
    except Exception as exc:
        errors.append(f"worktree prune after removal failed: {exc}")

    try:
        remove_tree(target_dir)
    except Exception as exc:
        errors.append(f"Cargo target directory removal failed: {exc}")

    if worktree.exists():
        errors.append(f"disposable worktree directory survived cleanup: {worktree}")
    if target_dir.exists():
        errors.append(f"target directory survived cleanup: {target_dir}")
    try:
        registrations = run(["git", "-C", str(source_repo), "worktree", "list", "--porcelain"])
        if str(worktree) in registrations:
            errors.append(f"worktree registration survived cleanup: {worktree}")
    except Exception as exc:
        errors.append(f"worktree registration verification failed: {exc}")

    if errors:
        raise RuntimeError("validation cleanup failed:\n" + "\n".join(errors))


def build_spec(token: str) -> TargetSpec:
    return TargetSpec(
        name=f"rex6-lane3-validator-{token}",
        repo=SOURCE_REPO,
        baseline_ref=BASELINE,
        build=[],
        test=[],
        bench={
            "probe": TIMED_PROBE,
            "example": BENCH_EXAMPLE,
            "pkg": PKG,
            "sample_prefix": "BENCH",
            "metric": "ns_per_logical_workload",
        },
        profile={},
        regions=["crates/mega-evm"],
        context={},
        objectives=[{"metric": "ns_per_logical_workload", "minimize": True}],
        goal=Goal("ns_per_logical_workload"),
        stop=Stop(),
        prompts={},
        differential={
            "probe": DIFF_PROBE,
            "pkg": PKG,
            "example": DIFF_EXAMPLE,
            "prefix": "DIFF",
        },
        timeout=3600,
    )


def validate_to_stage(stage: Path, token: str) -> dict:
    worktree = WORKTREE_PARENT / f"rex6-lane3-baseline-disposable-{token}"
    spec = build_spec(token)
    target = SpecTarget(spec)
    commands = [
        f"git -C {SOURCE_REPO} worktree add --detach {worktree} {BASELINE}",
        f"git -C {worktree} submodule update --init --recursive",
        "SpecTarget.write_probe + SpecTarget._cargo_run (raw BENCH stdout)",
        "SpecTarget._cargo_run(scale=0) (deterministic zero-scale rejection)",
        "SpecTarget.bench(scale=1) (ARO parser)",
        "SpecTarget.run_diff_probe (ARO injection/parser)",
        "SpecTarget._cargo_run x2 (full DIFF stdout byte comparison)",
    ]
    result: dict | None = None
    original: BaseException | None = None
    original_tb = None
    try:
        run(["git", "-C", str(SOURCE_REPO), "worktree", "add", "--detach", str(worktree), BASELINE])
        run(["git", "-C", str(worktree), "submodule", "update", "--init", "--recursive"])
        target.td_for(worktree)
        if os.environ.get("LANE3_INJECT_FAILURE") == "after-worktree-setup":
            raise RuntimeError("injected validator failure after worktree setup")

        target.write_probe(worktree, PKG, BENCH_EXAMPLE)
        bench_stdout = target._cargo_run(worktree, PKG, BENCH_EXAMPLE, scale=1)
        (stage / "bench.stdout").write_bytes(bench_stdout.encode())
        bench_lines = [line for line in bench_stdout.splitlines() if line.startswith("BENCH ")]
        if len(bench_lines) != 1:
            raise RuntimeError(f"expected one BENCH line, got {bench_lines!r}")
        bench_tokens = bench_lines[0].split()[1:]
        if len(bench_tokens) != 5 or not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", x) for x in bench_tokens):
            raise RuntimeError(f"BENCH line is not five numeric samples: {bench_lines[0]!r}")

        try:
            target._cargo_run(worktree, PKG, BENCH_EXAMPLE, scale=0)
        except RuntimeError as exc:
            if "ARO_BENCH_SCALE must be greater than zero" not in str(exc):
                raise RuntimeError(f"zero-scale rejection was not deterministic: {exc}") from exc
        else:
            raise RuntimeError("ARO_BENCH_SCALE=0 was accepted")

        parsed_samples = target.bench(worktree, scale=1).get("ns_per_logical_workload")
        if not parsed_samples or len(parsed_samples) != 5:
            raise RuntimeError(f"ARO BENCH parser returned {parsed_samples!r}")

        parsed_diff = target.run_diff_probe(worktree, spec.differential)
        if not parsed_diff or not re.fullmatch(r"DIFF [0-9a-f]{64}", parsed_diff):
            raise RuntimeError(f"ARO DIFF parser returned {parsed_diff!r}")
        diff_stdout_1 = target._cargo_run(worktree, PKG, DIFF_EXAMPLE)
        diff_stdout_2 = target._cargo_run(worktree, PKG, DIFF_EXAMPLE)
        (stage / "diff-run1.stdout").write_bytes(diff_stdout_1.encode())
        (stage / "diff-run2.stdout").write_bytes(diff_stdout_2.encode())
        if diff_stdout_1.encode() != diff_stdout_2.encode():
            raise RuntimeError("full DIFF stdout differs byte-for-byte")
        if diff_stdout_1 != parsed_diff + "\n":
            raise RuntimeError("full DIFF stdout is not exactly the parsed fingerprint plus newline")

        tracked_status = run(
            ["git", "-C", str(worktree), "status", "--short", "--untracked-files=no"]
        )
        if tracked_status:
            raise RuntimeError(f"detached target has tracked changes: {tracked_status!r}")
        result = {
            "aro_head": run(["git", "-C", str(ARO_ROOT), "rev-parse", "HEAD"]).strip(),
            "baseline": BASELINE,
            "bench_line": bench_lines[0],
            "bench_parser_samples": parsed_samples,
            "commands": commands,
            "diff_byte_identical": True,
            "diff_full_stdout_bytes": len(diff_stdout_1.encode()),
            "diff_parsed": parsed_diff,
            "logical_workload_policy": {
                "differential_outer_repetitions": 1,
                "internal_log_repetitions": 50,
                "internal_storage_repetitions": 100,
                "timed_outer_repetitions": "warmup/sampling/scale/spin measurement wrapper only",
            },
            "probe_sha256": {
                TIMED_PROBE: sha256(ARO_ROOT / TIMED_PROBE),
                DIFF_PROBE: sha256(ARO_ROOT / DIFF_PROBE),
            },
            "target_tracked_status": tracked_status,
            "zero_scale_rejected": True,
        }
    except BaseException as exc:
        original = exc
        original_tb = exc.__traceback__

    cleanup_error: BaseException | None = None
    try:
        cleanup_validation_run(SOURCE_REPO, worktree, target._td_root)
    except BaseException as exc:
        cleanup_error = exc

    if cleanup_error is not None:
        if original is not None:
            raise cleanup_error from original
        raise cleanup_error
    if original is not None:
        raise original.with_traceback(original_tb)
    if result is None:
        raise RuntimeError("validation completed without a result")
    result["target_dir_cleaned"] = True
    result["worktree_cleaned"] = True
    return result


def write_staged_evidence(stage: Path, result: dict) -> str:
    validation_path = stage / "validation.json"
    validation_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    entries = (
        (DATA_DIR / "validate.py", "validate.py"),
        (ARO_ROOT / TIMED_PROBE, "../../../../" + TIMED_PROBE),
        (ARO_ROOT / DIFF_PROBE, "../../../../" + DIFF_PROBE),
        (stage / "bench.stdout", "bench.stdout"),
        (stage / "diff-run1.stdout", "diff-run1.stdout"),
        (stage / "diff-run2.stdout", "diff-run2.stdout"),
        (validation_path, "validation.json"),
    )
    manifest = "".join(f"{sha256(path)}  {name}\n" for path, name in entries)
    (stage / "SHA256SUMS").write_text(manifest)
    return manifest


def remove_stage(stage: Path) -> None:
    remove_tree(stage)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_root = ARO_ROOT / ".aro-runs"
    lock_dir = run_root / "locks"
    staging_root = run_root / "staging"
    lock_dir.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    requested_token = os.environ.get("LANE3_RUN_TOKEN")
    token = requested_token or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", token):
        raise RuntimeError("LANE3_RUN_TOKEN must contain only safe path characters")

    lock_path = lock_dir / "mega-evm-rex6-lane3-selfdestruct.lock"
    with lock_path.open("a+") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        stage = Path(tempfile.mkdtemp(prefix=f"lane3-{token}-", dir=staging_root))
        try:
            result = validate_to_stage(stage, token)
            manifest = write_staged_evidence(stage, result)
            # Publication occurs only after validation and verified target cleanup.
            # The lock prevents readers running this validator from observing mixed runs;
            # SHA256SUMS is replaced last as the completeness marker.
            for name in FINAL_OUTPUTS:
                os.replace(stage / name, DATA_DIR / name)
            remove_stage(stage)
        except BaseException:
            try:
                remove_stage(stage)
            except BaseException as cleanup_exc:
                raise RuntimeError(f"staging cleanup failed: {stage}") from cleanup_exc
            raise
        finally:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    print((DATA_DIR / "validation.json").read_text(), end="")
    print("SHA256SUMS")
    print(manifest, end="")


if __name__ == "__main__":
    main()
