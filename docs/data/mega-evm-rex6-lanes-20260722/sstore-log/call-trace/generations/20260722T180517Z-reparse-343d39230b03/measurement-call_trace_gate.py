#!/usr/bin/env python3
"""Lane 1 dynamic Callgrind/editable-intersection gate.

This script deliberately does not create a TargetSpec file. It builds an in-memory
TargetSpec only to exercise ARO's real SpecTarget probe/package/target-dir paths.
Evidence is staged, the disposable target checkout is verified removed, and the
SHA-256 manifest is published last under the same Lane 1 advisory lock used by
validate.py.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve()
DATA_DIR = SCRIPT.parent
ARO_ROOT = SCRIPT.parents[5]
SOURCE_REPO = Path(os.environ.get("MEGA_EVM_ARO_REPO", "/home/mega-engineer/workspace/mega-evm-aro"))
WORKTREE_PARENT = Path(os.environ.get("LANE1_WORKTREE_PARENT", "/nvme2/mega-engineer/workspace"))
BASELINE = "996c16a91d071e3bb95780ea7dc5d4f1677bf746"
TIMED_PROBE = "probes/mega_evm_rex6_sstore_log.rs"
DIFF_PROBE = "probes/mega_evm_rex6_sstore_log_diff.rs"
PKG = "mega-evm"
TIMED_EXAMPLE = "aro_rex6_lane1_sstore_log"
DIFF_EXAMPLE = "aro_rex6_lane1_sstore_log_diff"
EXPECTED_DIFF_RE = re.compile(r"^DIFF [0-9a-f]{64}\n$")
EXPECTED_BENCH_RE = re.compile(r"^BENCH(?: [0-9]+(?:\.[0-9]+)?){5}$")
TARGET_PREFIX = "crates/mega-evm/src/"
TIMEOUT = int(os.environ.get("LANE1_CALLGRIND_TIMEOUT", "3600"))

# Relevance is applied only after dynamic intersection. These are target-owned
# production modules whose responsibilities directly implement the exercised
# SSTORE/SLOAD/LOG instruction, host, gas, and limit handling. A listed path is
# never proposed unless both raw traces independently contain positive-Ir evidence.
RELEVANT_RUNTIME_FILES = {
    "crates/mega-evm/src/constants.rs",
    "crates/mega-evm/src/evm/host.rs",
    "crates/mega-evm/src/evm/instructions.rs",
    "crates/mega-evm/src/external/gas.rs",
    "crates/mega-evm/src/limit/data_size.rs",
    "crates/mega-evm/src/limit/frame_limit.rs",
    "crates/mega-evm/src/limit/kv_update.rs",
    "crates/mega-evm/src/limit/limit.rs",
    "crates/mega-evm/src/limit/state_growth.rs",
    "crates/mega-evm/src/limit/storage_call_stipend.rs",
}

FINAL_NAMES = (
    "timed.callgrind.out",
    "differential.callgrind.out",
    "timed.calltree.txt",
    "differential.calltree.txt",
    "timed.stdout",
    "timed.stderr",
    "differential.stdout",
    "differential.stderr",
    "timed.reached.json",
    "differential.reached.json",
    "intersection.json",
    "proposed-editable.json",
    "run-metadata.json",
    "commands.json",
    "tool-fingerprint.json",
    "SHA256SUMS",
)

sys.path.insert(0, str(ARO_ROOT))
from aro.spec import Goal, Stop, TargetSpec  # noqa: E402
from aro.target import SpecTarget  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int = TIMEOUT, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, timeout=timeout,
                          check=True, text=True, capture_output=capture)


def output(command: list[str], *, cwd: Path | None = None,
           env: dict[str, str] | None = None, timeout: int = 120) -> str:
    return run(command, cwd=cwd, env=env, timeout=timeout).stdout.strip()


def safe_remove(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    if path.exists():
        raise RuntimeError(f"path survived removal: {path}")


def active_heavy_processes() -> list[dict[str, Any]]:
    uid = os.getuid()
    found: list[dict[str, Any]] = []
    heavy = {"cargo", "rustc", "valgrind", "callgrind_annotate"}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            if proc.stat().st_uid != uid:
                continue
            comm = (proc / "comm").read_text().strip()
            if comm not in heavy:
                continue
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            found.append({"pid": int(proc.name), "comm": comm, "cmdline": cmdline})
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return sorted(found, key=lambda x: x["pid"])


def build_spec(token: str) -> TargetSpec:
    return TargetSpec(
        name=f"rex6-lane1-calltrace-{token}", repo=SOURCE_REPO, baseline_ref=BASELINE,
        build=[], test=[],
        bench={"probe": TIMED_PROBE, "example": TIMED_EXAMPLE, "pkg": PKG,
               "sample_prefix": "BENCH", "metric": "ns_per_logical_workload"},
        profile={}, regions=["crates/mega-evm"], context={},
        objectives=[{"metric": "ns_per_logical_workload", "minimize": True}],
        goal=Goal("ns_per_logical_workload"), stop=Stop(), prompts={},
        differential={"probe": DIFF_PROBE, "pkg": PKG, "example": DIFF_EXAMPLE,
                      "prefix": "DIFF"}, timeout=TIMEOUT,
    )


def resolve_mapping(token: str, table: dict[str, str], previous: str | None) -> str:
    """Resolve Callgrind's `(id) value` compression while retaining prior context."""
    token = token.strip()
    match = re.match(r"^\(([^)]+)\)(?:\s+(.*))?$", token)
    if not match:
        return token
    key, value = match.group(1), match.group(2)
    if value is not None:
        table[key] = value
        return value
    return table.get(key, previous or "")


def normalize_target_source(raw: str) -> str | None:
    raw = raw.replace("\\", "/")
    pos = raw.find(TARGET_PREFIX)
    if pos < 0:
        return None
    path = raw[pos:].split(" ", 1)[0]
    if not path.endswith(".rs") or "/../" in path or path.startswith("/"):
        return None
    return path


def parse_cost_values(line: str, event_count: int) -> list[int] | None:
    parts = line.split()
    if len(parts) < event_count + 1:
        return None
    values: list[int] = []
    for token in parts[-event_count:]:
        if token == "*":
            values.append(0)
            continue
        try:
            values.append(int(token.replace(",", "")))
        except ValueError:
            return None
    return values


def parse_callgrind(path: Path) -> dict[str, Any]:
    events: list[str] = []
    totals: list[int] | None = None
    file_ids: dict[str, str] = {}
    fn_ids: dict[str, str] = {}
    current_file = ""
    current_fn = ""
    callee_file: str | None = None
    callee_fn: str | None = None
    pending_calls = 0
    evidence: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"self_ir": 0, "called_ir": 0, "call_count": 0, "locations": set()}
    )

    with path.open(errors="replace") as stream:
        for raw_line in stream:
            line = raw_line.rstrip("\n")
            if line.startswith("events:"):
                events = line.split(":", 1)[1].split()
            elif line.startswith("summary:"):
                try:
                    totals = [int(x) for x in line.split(":", 1)[1].split()]
                except ValueError:
                    totals = None
            elif line.startswith("fl=") or line.startswith("fi=") or line.startswith("fe="):
                current_file = resolve_mapping(line.split("=", 1)[1], file_ids, current_file)
                callee_file = callee_fn = None
                pending_calls = 0
            elif line.startswith("fn="):
                current_fn = resolve_mapping(line[3:], fn_ids, current_fn)
                callee_file = callee_fn = None
                pending_calls = 0
            elif line.startswith("cfl=") or line.startswith("cfi="):
                callee_file = resolve_mapping(line.split("=", 1)[1], file_ids, callee_file)
            elif line.startswith("cfn="):
                callee_fn = resolve_mapping(line[4:], fn_ids, callee_fn)
            elif line.startswith("calls="):
                try:
                    pending_calls = int(line[6:].split()[0])
                except (ValueError, IndexError):
                    pending_calls = 0
            elif line and (line[0].isdigit() or line[0] in "+-*") and events:
                values = parse_cost_values(line, len(events))
                if values is None or "Ir" not in events:
                    continue
                ir = values[events.index("Ir")]
                if ir <= 0:
                    pending_calls = 0
                    continue
                location = line.split()[0]
                if pending_calls > 0 and callee_file is not None:
                    source = normalize_target_source(callee_file)
                    if source:
                        key = (source, callee_fn or "<unknown>")
                        evidence[key]["called_ir"] += ir
                        evidence[key]["call_count"] += pending_calls
                        evidence[key]["locations"].add(location)
                    pending_calls = 0
                else:
                    source = normalize_target_source(current_file)
                    if source:
                        key = (source, current_fn or "<unknown>")
                        evidence[key]["self_ir"] += ir
                        evidence[key]["locations"].add(location)

    if "Ir" not in events:
        raise RuntimeError(f"{path.name}: Callgrind events do not contain Ir: {events!r}")
    total_ir = totals[events.index("Ir")] if totals and len(totals) == len(events) else None
    if not total_ir or total_ir <= 0:
        raise RuntimeError(f"{path.name}: missing/non-positive summary Ir: {total_ir!r}")

    rows = []
    for (source, function), item in sorted(evidence.items()):
        rows.append({"file": source, "function": function,
                     "self_ir": item["self_ir"], "called_ir": item["called_ir"],
                     "call_count": item["call_count"],
                     "locations": sorted(item["locations"])})
    files = sorted({row["file"] for row in rows if row["self_ir"] > 0 or row["called_ir"] > 0})
    if not files or not rows:
        raise RuntimeError(f"{path.name}: no positive-Ir target-owned Rust source/function mapping")
    return {"schema_version": 1, "raw_file": path.name, "events": events,
            "summary_ir": total_ir, "reached_files": files, "evidence": rows}


def parse_selftest() -> None:
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "callgrind.out"
        fixture.write_text("""version: 1\npositions: line\nevents: Ir\nfl=(1) /x/crates/mega-evm/src/evm/instructions.rs\nfn=(1) own\n10 7\ncfl=(2) /x/crates/mega-evm/src/evm/host.rs\ncfn=(2) called\ncalls=2 20\n20 11\nsummary: 18\n""")
        parsed = parse_callgrind(fixture)
        assert parsed["summary_ir"] == 18
        assert parsed["reached_files"] == [
            "crates/mega-evm/src/evm/host.rs",
            "crates/mega-evm/src/evm/instructions.rs",
        ]
        assert parsed["evidence"][0]["call_count"] == 2
    print("call-trace parser selftest: OK")


def command_identity(command: list[str], *, env: dict[str, str]) -> dict[str, Any]:
    """Capture version/help identity even for Valgrind Perl tools that exit 255."""
    completed = subprocess.run(command, env=env, timeout=120, text=True, capture_output=True)
    text = (completed.stdout or completed.stderr or "").strip()
    return {"argv": command, "exit_code": completed.returncode,
            "output": "\n".join(text.splitlines()[:20])}


def fingerprint_tools(valgrind: Path, annotate: Path, worktree: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["VALGRIND_LIB"] = str(Path.home() / ".local/libexec/valgrind")
    return {
        "cargo": output(["cargo", "-V"], cwd=worktree),
        "rustc_verbose": output(["rustc", "-Vv"], cwd=worktree),
        "git": output(["git", "--version"]),
        "python": sys.version,
        "platform": output(["uname", "-a"]),
        "valgrind_path": str(valgrind),
        "valgrind_sha256": sha256(valgrind),
        "valgrind_version": output([str(valgrind), "--version"], env=env),
        "valgrind_lib": env["VALGRIND_LIB"],
        "callgrind_annotate_path": str(annotate),
        "callgrind_annotate_sha256": sha256(annotate),
        "callgrind_annotate_identity": command_identity([str(annotate), "--help"], env=env),
    }


def cleanup(source_repo: Path, worktree: Path, target_root: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        registrations = output(["git", "-C", str(source_repo), "worktree", "list", "--porcelain"])
        if str(worktree) in registrations:
            run(["git", "-C", str(source_repo), "worktree", "remove", "--force", str(worktree)])
    except Exception as exc:
        errors.append(f"registered worktree removal failed: {exc}")
    try:
        safe_remove(worktree)
    except Exception as exc:
        errors.append(f"residual worktree removal failed: {exc}")
    try:
        run(["git", "-C", str(source_repo), "worktree", "prune"])
    except Exception as exc:
        errors.append(f"worktree prune failed: {exc}")
    try:
        safe_remove(target_root)
    except Exception as exc:
        errors.append(f"target-root removal failed: {exc}")
    registered = True
    try:
        registered = str(worktree) in output(
            ["git", "-C", str(source_repo), "worktree", "list", "--porcelain"])
    except Exception as exc:
        errors.append(f"registration verification failed: {exc}")
    if worktree.exists() or target_root.exists() or registered:
        errors.append(f"cleanup invariant failed: worktree_exists={worktree.exists()} "
                      f"target_exists={target_root.exists()} registered={registered}")
    if errors:
        raise RuntimeError("call-trace cleanup failed:\n" + "\n".join(errors))
    return {"worktree_absent": True, "target_root_absent": True, "worktree_unregistered": True}


def cargo_build(target: SpecTarget, worktree: Path, example: str,
                commands: list[dict[str, Any]], stage: Path) -> Path:
    env = target.env_for(worktree, measurement_kind="icount")
    env["CARGO_BUILD_JOBS"] = "1"
    command = ["cargo", "build", "--release", "-p", PKG, "--example", example,
               "--message-format=json"]
    commands.append({"name": f"build-{example}", "cwd": str(worktree), "argv": command,
                     "env": {k: env[k] for k in ("CARGO_TARGET_DIR", "CARGO_PROFILE_RELEASE_DEBUG",
                                                   "CARGO_PROFILE_RELEASE_STRIP", "CARGO_BUILD_JOBS",
                                                   "RAYON_NUM_THREADS")}})
    completed = run(command, cwd=worktree, env=env)
    (stage / f"build-{example}.jsonl").write_text(completed.stdout)
    (stage / f"build-{example}.stderr").write_text(completed.stderr)
    executable = None
    for line in completed.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        target_info = message.get("target", {})
        if (message.get("reason") == "compiler-artifact" and message.get("executable")
                and target_info.get("name") == example
                and "example" in (target_info.get("kind") or [])):
            executable = Path(message["executable"])
    if executable is None or not executable.is_file():
        raise RuntimeError(f"cargo did not report a usable executable for {example}: {executable}")
    return executable


def run_trace(label: str, binary: Path, raw_path: Path, stdout_path: Path, stderr_path: Path,
              valgrind: Path, worktree: Path, target: SpecTarget,
              commands: list[dict[str, Any]]) -> None:
    env = target.env_for(worktree, measurement_kind="icount")
    env.update({"ARO_BENCH_SCALE": "1", "RAYON_NUM_THREADS": "1",
                "VALGRIND_LIB": str(Path.home() / ".local/libexec/valgrind")})
    command = [str(valgrind), "--tool=callgrind", "--collect-atstart=yes",
               "--compress-strings=yes", "--compress-pos=yes",
               f"--callgrind-out-file={raw_path}", str(binary)]
    commands.append({"name": f"trace-{label}", "cwd": str(worktree), "argv": command,
                     "env": {k: env[k] for k in ("ARO_BENCH_SCALE", "RAYON_NUM_THREADS",
                                                   "VALGRIND_LIB", "CARGO_TARGET_DIR")}})
    started = time.time()
    completed = subprocess.run(command, cwd=worktree, env=env, timeout=TIMEOUT,
                               text=True, capture_output=True)
    stdout_path.write_text(completed.stdout)
    stderr_path.write_text(completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"{label} Callgrind exit {completed.returncode}: {completed.stderr[-4000:]}")
    if not raw_path.is_file() or raw_path.stat().st_size == 0:
        raise RuntimeError(f"{label} Callgrind raw output missing/empty")
    commands[-1]["elapsed_seconds"] = round(time.time() - started, 3)


def annotate_trace(label: str, raw: Path, destination: Path, annotate: Path,
                   commands: list[dict[str, Any]]) -> None:
    env = dict(os.environ)
    env["VALGRIND_LIB"] = str(Path.home() / ".local/libexec/valgrind")
    command = [str(annotate), "--show=Ir", "--inclusive=yes", "--tree=both",
               "--threshold=0.1", str(raw)]
    commands.append({"name": f"annotate-{label}", "cwd": str(DATA_DIR), "argv": command,
                     "env": {"VALGRIND_LIB": env["VALGRIND_LIB"]}})
    completed = run(command, cwd=DATA_DIR, env=env)
    destination.write_text(completed.stdout)
    if not completed.stdout.strip():
        raise RuntimeError(f"{label} callgrind_annotate produced empty call tree")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def manifest(stage: Path) -> str:
    entries = [
        (SCRIPT, "call_trace_gate.py"),
        (ARO_ROOT / TIMED_PROBE, "../../../../../" + TIMED_PROBE),
        (ARO_ROOT / DIFF_PROBE, "../../../../../" + DIFF_PROBE),
    ]
    entries.extend((stage / name, name) for name in FINAL_NAMES if name != "SHA256SUMS")
    entries.extend((p, p.name) for p in sorted(stage.glob("build-*")))
    text = "".join(f"{sha256(path)}  {name}\n" for path, name in entries)
    (stage / "SHA256SUMS").write_text(text)
    return text


def execute() -> dict[str, Any]:
    token = os.environ.get("LANE1_RUN_TOKEN") or f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", token):
        raise RuntimeError("LANE1_RUN_TOKEN must contain only safe path characters")
    valgrind = Path.home() / ".local/bin/valgrind"
    annotate = Path.home() / ".local/bin/callgrind_annotate"
    for tool in (valgrind, annotate):
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise RuntimeError(f"pinned tool is missing/not executable: {tool}")

    run_root = ARO_ROOT / ".aro-runs"
    lock_dir = run_root / "locks"
    staging_root = run_root / "staging"
    lock_dir.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "mega-evm-rex6-lane1-sstore-log.lock"

    with lock_path.open("a+") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        busy = active_heavy_processes()
        if busy:
            raise RuntimeError(f"host is not quiet; refusing serial trace: {busy}")
        stage = Path(tempfile.mkdtemp(prefix=f"lane1-calltrace-{token}-", dir=staging_root))
        worktree = WORKTREE_PARENT / f"rex6-lane1-calltrace-disposable-{token}"
        commands: list[dict[str, Any]] = []
        target = SpecTarget(build_spec(token))
        result: dict[str, Any] | None = None
        original: BaseException | None = None
        original_tb = None
        cleanup_result: dict[str, Any] | None = None
        try:
            add = ["git", "-C", str(SOURCE_REPO), "worktree", "add", "--detach",
                   str(worktree), BASELINE]
            commands.append({"name": "worktree-add", "cwd": str(ARO_ROOT), "argv": add})
            run(add)
            submodule = ["git", "-C", str(worktree), "submodule", "update", "--init", "--recursive"]
            commands.append({"name": "submodule-init", "cwd": str(ARO_ROOT), "argv": submodule})
            run(submodule)
            actual_baseline = output(["git", "-C", str(worktree), "rev-parse", "HEAD"])
            if actual_baseline != BASELINE:
                raise RuntimeError(f"detached baseline mismatch: {actual_baseline}")

            # Real ARO path resolution and source loaders; no guessed crate path.
            target.write_probe(worktree, PKG, TIMED_EXAMPLE)
            pkg_dir = target.pkg_dir(worktree, PKG)
            diff_path = pkg_dir / "examples" / f"{DIFF_EXAMPLE}.rs"
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            diff_path.write_text(target.spec.diff_probe_src())
            if sha256(pkg_dir / "examples" / f"{TIMED_EXAMPLE}.rs") != sha256(ARO_ROOT / TIMED_PROBE):
                raise RuntimeError("SpecTarget timed injection does not match tracked probe")
            if sha256(diff_path) != sha256(ARO_ROOT / DIFF_PROBE):
                raise RuntimeError("SpecTarget differential injection does not match tracked probe")

            tools = fingerprint_tools(valgrind, annotate, worktree)
            timed_binary = cargo_build(target, worktree, TIMED_EXAMPLE, commands, stage)
            diff_binary = cargo_build(target, worktree, DIFF_EXAMPLE, commands, stage)
            binaries = {
                "timed": {"path": str(timed_binary), "sha256": sha256(timed_binary)},
                "differential": {"path": str(diff_binary), "sha256": sha256(diff_binary)},
            }

            # Intentionally sequential: never overlap Callgrind jobs.
            run_trace("timed", timed_binary, stage / "timed.callgrind.out",
                      stage / "timed.stdout", stage / "timed.stderr", valgrind,
                      worktree, target, commands)
            run_trace("differential", diff_binary, stage / "differential.callgrind.out",
                      stage / "differential.stdout", stage / "differential.stderr", valgrind,
                      worktree, target, commands)

            timed_stdout = (stage / "timed.stdout").read_text()
            timed_bench = [line for line in timed_stdout.splitlines() if line.startswith("BENCH ")]
            if len(timed_bench) != 1 or not EXPECTED_BENCH_RE.fullmatch(timed_bench[0]):
                raise RuntimeError(f"timed trace lacked expected BENCH output: {timed_stdout!r}")
            diff_stdout = (stage / "differential.stdout").read_text()
            if not EXPECTED_DIFF_RE.fullmatch(diff_stdout):
                raise RuntimeError(f"differential trace lacked exact DIFF output: {diff_stdout!r}")

            annotate_trace("timed", stage / "timed.callgrind.out",
                           stage / "timed.calltree.txt", annotate, commands)
            annotate_trace("differential", stage / "differential.callgrind.out",
                           stage / "differential.calltree.txt", annotate, commands)
            timed = parse_callgrind(stage / "timed.callgrind.out")
            differential = parse_callgrind(stage / "differential.callgrind.out")
            write_json(stage / "timed.reached.json", timed)
            write_json(stage / "differential.reached.json", differential)
            common = sorted(set(timed["reached_files"]) & set(differential["reached_files"]))
            timed_only = sorted(set(timed["reached_files"]) - set(differential["reached_files"]))
            differential_only = sorted(set(differential["reached_files"]) - set(timed["reached_files"]))
            if not common:
                raise RuntimeError("dynamic target-owned file intersection is empty")
            intersection = {"schema_version": 1, "timed_summary_ir": timed["summary_ir"],
                            "differential_summary_ir": differential["summary_ir"],
                            "common_files": common, "timed_only_files": timed_only,
                            "differential_only_files": differential_only}
            write_json(stage / "intersection.json", intersection)

            editable = sorted(set(common) & RELEVANT_RUNTIME_FILES)
            common_excluded = sorted(set(common) - set(editable))
            differential_excluded = differential_only
            if not editable:
                raise RuntimeError("no dynamically common SSTORE/SLOAD/LOG production file remains")
            proposed = {
                "schema_version": 1,
                "gate": "call-trace/editable-intersection",
                "gate_passed": True,
                "mutation_gate_passed": False,
                "mutation_status": "NOT RUN; target spec creation remains blocked",
                "selection_rule": "dynamic common target-owned files intersected with explicit SSTORE/SLOAD/LOG production-handler relevance set",
                "proposed_editable": editable,
                "common_harness_or_lifecycle_excluded": common_excluded,
                "one_sided_timed_excluded": timed_only,
                "one_sided_differential_hashing_or_framework_excluded": differential_excluded,
                "runtime_evidence_files": ["timed.reached.json", "differential.reached.json", "intersection.json"],
            }
            write_json(stage / "proposed-editable.json", proposed)
            write_json(stage / "commands.json", commands)
            write_json(stage / "tool-fingerprint.json", tools)
            tracked_status = output(["git", "-C", str(worktree), "status", "--short",
                                     "--untracked-files=no"])
            if tracked_status:
                raise RuntimeError(f"detached baseline gained tracked changes: {tracked_status!r}")
            result = {
                "schema_version": 1, "status": "passed", "aro_head": output(
                    ["git", "-C", str(ARO_ROOT), "rev-parse", "HEAD"]),
                "baseline": BASELINE, "actual_detached_baseline": actual_baseline,
                "probe_sha256": {TIMED_PROBE: sha256(ARO_ROOT / TIMED_PROBE),
                                  DIFF_PROBE: sha256(ARO_ROOT / DIFF_PROBE)},
                "script_sha256": sha256(SCRIPT), "binaries": binaries,
                "bench_line": timed_bench[0], "diff_line": diff_stdout.strip(),
                "timed_ir": timed["summary_ir"], "differential_ir": differential["summary_ir"],
                "proposed_editable": editable, "target_tracked_status": tracked_status,
                "host_quiet_before": True,
            }
        except BaseException as exc:
            original, original_tb = exc, exc.__traceback__
        try:
            cleanup_result = cleanup(SOURCE_REPO, worktree, target._td_root)
        except BaseException as cleanup_exc:
            if original is not None:
                raise cleanup_exc from original
            raise
        if original is not None:
            safe_remove(stage)
            raise original.with_traceback(original_tb)
        assert result is not None and cleanup_result is not None
        result["cleanup"] = cleanup_result
        result["host_quiet_after"] = not active_heavy_processes()
        if not result["host_quiet_after"]:
            safe_remove(stage)
            raise RuntimeError("heavy process survived completed trace")
        write_json(stage / "run-metadata.json", result)
        # commands/tool files were written before cleanup; rewrite commands to bind elapsed values.
        write_json(stage / "commands.json", commands)
        manifest_text = manifest(stage)
        for name in FINAL_NAMES:
            os.replace(stage / name, DATA_DIR / name)
        for generated in stage.glob("build-*"):
            os.replace(generated, DATA_DIR / generated.name)
        safe_remove(stage)
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
    return result | {"manifest": manifest_text}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test-parser", action="store_true")
    args = parser.parse_args()
    if args.self_test_parser:
        parse_selftest()
        return
    result = execute()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
