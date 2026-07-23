#!/usr/bin/env python3
"""Fail-closed, trial-resumable Salt PR #148 verification runner.

No shell is used. Every subprocess is isolated, sanitized, cleaned, reaped, and
atomically represented by an immutable attempted-run record. Campaign execution
requires --run and an exclusive evidence-directory lock.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

UTC = dt.timezone.utc
HERE = Path(__file__).resolve().parent
DEFAULT_ARO = Path("/nvme2/mega-engineer/workspace/aro-salt-livelock-verify")
DEFAULT_CONTROL = Path("/nvme2/mega-engineer/workspace/salt-pr148-control")
DEFAULT_PR = Path("/nvme2/mega-engineer/workspace/salt-pr148-experiment")
RUN_ROOT_REL = Path(".aro-runs/salt-pr148-verify")
CONTROL_SHA = "19419f4d13e6c615b7a94cf3d2bf53d1052f723c"
PR_SHA = "ff8442f5413e6bf444af1b26f8f82b752db09475"
CONTROL_TREE = "31dc0405c080ad366bdf1f99531e8f5d0d8b493c"
PR_TREE = "c1d82feaa3b54e17cf5abfa710bcf0405500577b"
CARGO_LOCK_SHA256 = "539e8ecdfda09b7267b5a6104fe368b804f113dd171b7451808d077113c8e1a9"
ORIGIN_URL = "https://github.com/megaeth-labs/salt.git"
CAMPAIGN_ID = "salt-pr148-livelock-verify-20260723-v2"
RUNTIME_TIMEOUT = 300.0
COMPILE_TIMEOUT = 900.0
TERM_GRACE = 10.0
BASELINE_TEST = "trie::trie::tests::test_create_node_aligned_chunks_never_splits_duplicate_node"
PHASES = ("metadata", "prebuild", "control", "experiment", "regressions", "conformance", "init-cost", "finalize")
RESIZE_ENV = {"NUM_DATA_BUCKETS": "2", "BUCKET_RESIZE_LOAD_FACTOR_PCT": "1"}
RANDOM_ENV = {
    "NUM_DATA_BUCKETS": "2", "BUCKET_RESIZE_LOAD_FACTOR_PCT": "80",
    "RANDOM_KV_POOL_SIZE": "4096", "RANDOM_ITERATIONS": "100", "RANDOM_BLOCKS": "3",
    "RANDOM_MINI_BLOCKS": "10", "RANDOM_OPS": "100", "RANDOM_LOOKUPS": "50",
}


def utc_now() -> str:
    return dt.datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def atomic_write_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode())


def percentile95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    exact = {"GH_TOKEN", "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "NETRC"}
    return upper in exact or any(token in upper for token in ("PASSWORD", "SECRET", "AUTH_TOKEN", "API_KEY", "PRIVATE_KEY"))


def is_contaminating_name(name: str) -> bool:
    upper = name.upper()
    exact = {
        "RUST_TEST_THREADS", "RUSTUP_TOOLCHAIN", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_HOME",
        "NUM_DATA_BUCKETS", "BUCKET_RESIZE_LOAD_FACTOR_PCT", "MAKEFLAGS", "MFLAGS", "NUM_JOBS",
    }
    prefixes = ("RANDOM_", "RAYON_", "TOKIO_", "CARGO_")
    return upper in exact or upper.startswith(prefixes) or is_secret_name(upper)


def sanitized_environment(delta: dict[str, str] | None) -> tuple[dict[str, str], list[str], str]:
    explicit = {str(k): str(v) for k, v in (delta or {}).items()}
    secret_explicit = sorted(k for k in explicit if is_secret_name(k))
    if secret_explicit:
        raise ValueError(f"explicit secret environment variables forbidden: {secret_explicit}")
    full: dict[str, str] = {}
    stripped: list[str] = []
    for key, value in os.environ.items():
        if is_contaminating_name(key):
            stripped.append(key)
        else:
            full[key] = value
    full.update(explicit)
    safe_fingerprint = canonical_hash(sorted(full.items()))
    return full, sorted(stripped), safe_fingerprint


def command_identity(argv: Sequence[str], env: dict[str, str], cwd: str) -> str:
    return canonical_hash({"argv": list(argv), "cwd": cwd, "explicit_env": env, "environment_policy": "sanitize-v2"})


@dataclass
class RunRecord:
    run_id: str
    trial_key: str
    phase: str
    cohort: str
    command: list[str]
    explicit_env: dict[str, str]
    stripped_env_names: list[str]
    safe_env_fingerprint: str
    identity_sha256: str
    cwd: str
    start_utc: str
    leader_end_utc: str
    end_utc: str
    process_elapsed_seconds: float
    cleanup_elapsed_seconds: float
    elapsed_seconds: float
    exit_code: int | None
    timeout: bool
    status: str
    error: str | None
    log_path: str
    log_sha256: str
    process_group_id: int | None
    cleanup_actions: list[str] = field(default_factory=list)
    residual_pids: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DuplicateTrialKey(RuntimeError):
    pass


class EvidenceStore:
    """Immutable records keyed by deterministic trial_key, plus atomic JSONL index."""

    def __init__(self, evidence_dir: Path):
        self.root = Path(evidence_dir)
        self.logs = self.root / "logs"
        self.records = self.root / "run-records"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.records.mkdir(parents=True, exist_ok=True)

    def existing(self) -> list[dict[str, Any]]:
        return [json.loads(path.read_text()) for path in sorted(self.records.glob("*.json"))]

    def by_trial_key(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in self.existing():
            key = row["trial_key"]
            if key in result:
                raise DuplicateTrialKey(f"duplicate immutable trial_key on disk: {key}")
            result[key] = row
        return result

    def assert_available(self, trial_key: str) -> None:
        if trial_key in self.by_trial_key():
            raise DuplicateTrialKey(f"trial_key already recorded: {trial_key}")

    def _target(self, trial_key: str) -> Path:
        return self.records / f"{hashlib.sha256(trial_key.encode()).hexdigest()}.json"

    def add(self, record: RunRecord) -> None:
        self.assert_available(record.trial_key)
        target = self._target(record.trial_key)
        if target.exists():
            raise DuplicateTrialKey(f"record target already exists: {target}")
        atomic_write_json(target, record.to_dict())
        self.rebuild_jsonl()

    def persist_attempt(self, record: RunRecord) -> None:
        """Persist even if normal add/index finalization fails."""
        try:
            self.add(record)
            return
        except DuplicateTrialKey:
            raise
        except Exception as exc:
            record.error = f"{record.error}; EvidenceStore.add={exc!r}" if record.error else f"EvidenceStore.add={exc!r}"
            target = self._target(record.trial_key)
            if not target.exists():
                atomic_write_json(target, record.to_dict())
            with contextlib.suppress(Exception):
                self.rebuild_jsonl()

    def rebuild_jsonl(self) -> None:
        payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in self.existing())
        atomic_write_text(self.root / "results.jsonl", payload)


class CampaignLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: Any = None

    def __enter__(self) -> CampaignLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = self.path.open("a+")
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.fd.close()
            raise RuntimeError(f"another campaign owns lock {self.path}") from exc
        self.fd.seek(0)
        self.fd.truncate()
        self.fd.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "acquired_utc": utc_now()}) + "\n")
        self.fd.flush()
        os.fsync(self.fd.fileno())
        return self

    def __exit__(self, *_: Any) -> None:
        if self.fd:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
            self.fd.close()


def _proc_stat(pid: int) -> tuple[int, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        rest = raw[raw.rfind(")") + 2 :].split()
        return int(rest[1]), int(rest[2])  # ppid, pgrp
    except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
        return None


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class CommandRunner:
    def __init__(self, store: EvidenceStore, scan_roots: Sequence[Path], runtime_timeout: float = RUNTIME_TIMEOUT, term_grace: float = TERM_GRACE):
        self.store = store
        self.scan_roots = tuple(Path(path).resolve() for path in scan_roots)
        self.runtime_timeout = runtime_timeout
        self.term_grace = term_grace

    def _members(self, pgrp: int) -> list[int]:
        members = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit() and (stat := _proc_stat(int(entry.name))) and stat[1] == pgrp:
                members.append(int(entry.name))
        return sorted(members)

    def scan_residuals(self, *, pgrp: int | None = None, leader_pid: int | None = None, exclude: Iterable[int] = ()) -> list[dict[str, Any]]:
        excluded = {os.getpid(), os.getppid(), *exclude}
        stats: dict[int, tuple[int, int]] = {}
        entries = [entry for entry in Path("/proc").iterdir() if entry.name.isdigit()]
        for entry in entries:
            pid = int(entry.name)
            if stat := _proc_stat(pid):
                stats[pid] = stat

        def descended(pid: int) -> bool:
            seen = set()
            while pid in stats and pid not in seen:
                seen.add(pid)
                parent = stats[pid][0]
                if parent == leader_pid:
                    return True
                pid = parent
            return False

        found = []
        for entry in entries:
            pid = int(entry.name)
            if pid in excluded:
                continue
            try:
                cwd = Path(os.readlink(entry / "cwd")).resolve()
                argv = [os.fsdecode(x) for x in (entry / "cmdline").read_bytes().split(b"\0") if x]
            except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                continue
            exact_path_match = any(_path_within(cwd, root) or any(Path(arg).is_absolute() and _path_within(Path(arg).resolve(), root) for arg in argv) for root in self.scan_roots)
            group_match = pgrp is not None and stats.get(pid, (0, -1))[1] == pgrp
            if group_match or descended(pid) or exact_path_match:
                found.append({"pid": pid, "ppid": stats.get(pid, (None, None))[0], "pgrp": stats.get(pid, (None, None))[1], "argv": argv, "cwd": str(cwd)})
        return sorted(found, key=lambda row: row["pid"])

    def _terminate(self, pgrp: int, out: Any, actions: list[str]) -> None:
        members = self._members(pgrp)
        if not members:
            return
        actions.append(f"SIGTERM pgrp={pgrp} members={members}")
        out.write((actions[-1] + "\n").encode())
        out.flush()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgrp, signal.SIGTERM)
        deadline = time.monotonic() + self.term_grace
        while time.monotonic() < deadline and self._members(pgrp):
            time.sleep(0.01)
        members = self._members(pgrp)
        if members:
            actions.append(f"SIGKILL pgrp={pgrp} members={members}")
            out.write((actions[-1] + "\n").encode())
            out.flush()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgrp, signal.SIGKILL)
        deadline = time.monotonic() + max(1.0, self.term_grace)
        while time.monotonic() < deadline and self._members(pgrp):
            time.sleep(0.01)

    def cleanup_preexisting(self) -> None:
        residuals = self.scan_residuals()
        for row in residuals:
            pgrp = row.get("pgrp")
            if isinstance(pgrp, int) and pgrp > 1 and pgrp != os.getpgrp():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgrp, signal.SIGTERM)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(row["pid"], signal.SIGTERM)
        if residuals:
            time.sleep(min(self.term_grace, 0.2))
            for row in self.scan_residuals():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(row["pid"], signal.SIGKILL)
            time.sleep(0.05)
        remaining = self.scan_residuals()
        if remaining:
            raise RuntimeError(f"pre-run process residuals could not be removed: {remaining}")

    def run(self, *, trial_key: str, phase: str, cohort: str, argv: Sequence[str], cwd: Path,
            env_delta: dict[str, str] | None = None, timeout: float | None = None,
            context: dict[str, Any] | None = None) -> RunRecord:
        if not argv or isinstance(argv, (str, bytes)):
            raise TypeError("argv must be a non-empty sequence, never a shell string")
        self.store.assert_available(trial_key)
        args = [str(item) for item in argv]
        delta = {str(key): str(value) for key, value in (env_delta or {}).items()}
        full_env, stripped, fingerprint = sanitized_environment(delta)
        cwd = Path(cwd).resolve()
        identity = command_identity(args, delta, str(cwd))
        run_id = hashlib.sha256(trial_key.encode()).hexdigest()[:16]
        log = self.store.logs / f"{run_id}.log"
        if log.exists():
            raise RuntimeError(f"unrecorded pre-existing log blocks trial: {log}")
        limit = self.runtime_timeout if timeout is None else timeout
        started_utc = utc_now()
        mono_start = time.monotonic()
        leader_end_mono = mono_start
        leader_end_utc = started_utc
        cleanup_start = mono_start
        timed_out = False
        exit_code: int | None = None
        status = "attempted-error"
        error: str | None = None
        proc: subprocess.Popen[bytes] | None = None
        pgrp: int | None = None
        actions: list[str] = []
        residuals: list[dict[str, Any]] = []
        pending: BaseException | None = None
        log_hash = ""
        out: Any = None
        try:
            self.cleanup_preexisting()
            out = log.open("xb")
            header = {
                "trial_key": trial_key, "argv": args, "cwd": str(cwd), "explicit_env": delta,
                "stripped_env_names": stripped, "safe_env_fingerprint": fingerprint,
                "identity_sha256": identity, "timeout_seconds": limit, "start_utc": started_utc,
            }
            out.write((json.dumps(header, sort_keys=True) + "\n").encode())
            out.flush()
            proc = subprocess.Popen(
                args, cwd=cwd, env=full_env, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, start_new_session=True, shell=False,
            )
            pgrp = os.getpgid(proc.pid)
            try:
                exit_code = proc.wait(timeout=limit)
                status = "pass" if exit_code == 0 else "fail"
            except subprocess.TimeoutExpired:
                timed_out = True
                status = "timeout"
            except BaseException as exc:
                pending = exc
                status = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "wait-error"
                error = repr(exc)
            finally:
                leader_end_mono = time.monotonic()
                leader_end_utc = utc_now()
                cleanup_start = leader_end_mono
                if pgrp is not None:
                    self._terminate(pgrp, out, actions)
                if proc.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=max(1.0, self.term_grace))
        except BaseException as exc:
            if pending is None:
                pending = exc if isinstance(exc, (KeyboardInterrupt, SystemExit)) else None
                status = "interrupted" if pending else "launch-error"
                error = repr(exc)
            leader_end_mono = time.monotonic()
            leader_end_utc = utc_now()
            cleanup_start = leader_end_mono
        finally:
            if proc is not None and pgrp is not None and out is not None:
                with contextlib.suppress(Exception):
                    self._terminate(pgrp, out, actions)
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.wait(timeout=max(1.0, self.term_grace))
            if out is not None:
                with contextlib.suppress(Exception):
                    out.flush()
                    os.fsync(out.fileno())
                    out.close()
            deadline = time.monotonic() + max(1.0, self.term_grace)
            while True:
                residuals = self.scan_residuals(pgrp=pgrp, leader_pid=proc.pid if proc else None, exclude=(proc.pid,) if proc else ())
                if not residuals or time.monotonic() >= deadline:
                    break
                for row in residuals:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(row["pid"], signal.SIGKILL)
                time.sleep(0.02)
            if residuals:
                status = "residual-fail"
            ended_mono = time.monotonic()
            ended_utc = utc_now()
            try:
                log_hash = sha256_file(log)
            except Exception as exc:
                error = f"{error}; log_hash={exc!r}" if error else f"log_hash={exc!r}"
                status = "evidence-error"
            process_elapsed = max(0.0, leader_end_mono - mono_start)
            cleanup_elapsed = max(0.0, ended_mono - cleanup_start)
            record = RunRecord(
                run_id=run_id, trial_key=trial_key, phase=phase, cohort=cohort, command=args,
                explicit_env=delta, stripped_env_names=stripped, safe_env_fingerprint=fingerprint,
                identity_sha256=identity, cwd=str(cwd), start_utc=started_utc,
                leader_end_utc=leader_end_utc, end_utc=ended_utc,
                process_elapsed_seconds=process_elapsed, cleanup_elapsed_seconds=cleanup_elapsed,
                elapsed_seconds=process_elapsed, exit_code=exit_code, timeout=timed_out, status=status,
                error=error, log_path=str(log.relative_to(self.store.root)), log_sha256=log_hash,
                process_group_id=pgrp, cleanup_actions=actions, residual_pids=residuals, context=context or {},
            )
            self.store.persist_attempt(record)
        if pending is not None:
            raise pending
        return record

    def capture(self, **kwargs: Any) -> dict[str, Any]:
        record = self.run(**kwargs)
        lines = (self.store.root / record.log_path).read_text(errors="replace").splitlines(keepends=True)
        output = "".join(lines[1:])
        return {"stdout": output, "record": record.to_dict()}


def resume_completed(record: dict[str, Any], *, fail_closed: bool, abort_prior_failure: bool = False) -> bool:
    clean = not record.get("residual_pids")
    status = record.get("status")
    if fail_closed:
        if status == "pass" and clean:
            return True
        if abort_prior_failure:
            raise RuntimeError(f"prior fail-closed trial blocks resume: {record.get('trial_key')} status={status}")
        return False
    return clean and status in {"pass", "fail", "timeout"}


def _entry(argv: list[str], env: dict[str, str], count: int, cohort: str, timeout: float, cwd: Path) -> dict[str, Any]:
    result = {"argv": argv, "env": env, "count": count, "cohort": cohort, "timeout_seconds": timeout}
    result["identity_sha256"] = command_identity(argv, {"CARGO_TARGET_DIR": str(DEFAULT_ARO / RUN_ROOT_REL / cohort), **env}, str(cwd))
    return result


def campaign_plan() -> dict[str, Any]:
    control_target = str(DEFAULT_ARO / RUN_ROOT_REL / "control")
    pr_target = str(DEFAULT_ARO / RUN_ROOT_REL / "pr")
    control = {
        "ordinary": _entry(["cargo", "test", "--locked"], {}, 5, "control", RUNTIME_TIMEOUT, DEFAULT_CONTROL),
        "resize": _entry(["cargo", "test", "--locked", "--features", "test-bucket-resize"], RESIZE_ENV, 5, "control", RUNTIME_TIMEOUT, DEFAULT_CONTROL),
    }
    experiment = {
        "ordinary": _entry(["cargo", "test", "--locked"], {}, 15, "pr", RUNTIME_TIMEOUT, DEFAULT_PR),
        "resize": _entry(["cargo", "test", "--locked", "--features", "test-bucket-resize"], RESIZE_ENV, 15, "pr", RUNTIME_TIMEOUT, DEFAULT_PR),
    }
    regressions = {
        name: _entry(["cargo", "test", "--locked", "-p", "salt", "--test", name, "--", "--nocapture"], {}, 50, "pr", RUNTIME_TIMEOUT, DEFAULT_PR)
        for name in ("shared_committer_init", "shared_committer_init_os_winner")
    }
    conformance_specs = [
        ("check", ["cargo", "check", "--locked", "--all-targets"], {}, COMPILE_TIMEOUT),
        ("cargo-sort", ["cargo", "sort", "--check", "--workspace", "--grouped", "--order", "package,workspace,lints,profile,bin,benches,dependencies,dev-dependencies,features"], {}, RUNTIME_TIMEOUT),
        ("test", ["cargo", "test", "--locked"], {}, RUNTIME_TIMEOUT),
        ("test-bucket-resize", ["cargo", "test", "--locked", "--features", "test-bucket-resize"], RESIZE_ENV, RUNTIME_TIMEOUT),
        ("random-stress", ["cargo", "test", "--locked", "-p", "salt", "--features", "test-bucket-resize", "test_e2e_random_stress", "--", "--ignored", "--nocapture"], RANDOM_ENV, RUNTIME_TIMEOUT),
        ("no-std-check", ["cargo", "+nightly-2026-03-20", "check", "--locked", "-p", "salt", "--target", "riscv64imac-unknown-none-elf", "--no-default-features"], {}, COMPILE_TIMEOUT),
        ("no-default-features-test", ["cargo", "+nightly-2026-03-20", "test", "--locked", "--no-default-features"], {}, RUNTIME_TIMEOUT),
        ("no-default-features-resize-test", ["cargo", "+nightly-2026-03-20", "test", "--locked", "--no-default-features", "--features", "test-bucket-resize"], {}, RUNTIME_TIMEOUT),
        ("fmt", ["cargo", "fmt", "--all", "--", "--check"], {}, RUNTIME_TIMEOUT),
        ("clippy", ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"], {}, COMPILE_TIMEOUT),
    ]
    conformance = []
    for name, argv, env, timeout in conformance_specs:
        item = _entry(argv, env, 1, "pr", timeout, DEFAULT_PR)
        item["name"] = name
        conformance.append(item)
    prebuild_specs = [
        ("control", "ordinary", ["cargo", "test", "--locked", "--no-run", "--message-format=json"], {}),
        ("control", "resize", ["cargo", "test", "--locked", "--features", "test-bucket-resize", "--no-run", "--message-format=json"], RESIZE_ENV),
        ("pr", "ordinary", ["cargo", "test", "--locked", "--no-run", "--message-format=json"], {}),
        ("pr", "resize", ["cargo", "test", "--locked", "--features", "test-bucket-resize", "--no-run", "--message-format=json"], RESIZE_ENV),
        ("pr", "no-default", ["cargo", "+nightly-2026-03-20", "test", "--locked", "--no-default-features", "--no-run", "--message-format=json"], {}),
        ("pr", "no-default-resize", ["cargo", "+nightly-2026-03-20", "test", "--locked", "--no-default-features", "--features", "test-bucket-resize", "--no-run", "--message-format=json"], {}),
    ]
    prebuild = []
    for cohort, name, argv, env in prebuild_specs:
        item = _entry(argv, env, 1, cohort, COMPILE_TIMEOUT, DEFAULT_CONTROL if cohort == "control" else DEFAULT_PR)
        item["name"] = name
        prebuild.append(item)
    return {
        "schema_version": 2, "campaign_id": CAMPAIGN_ID, "sequential": True,
        "runtime_timeout_seconds": RUNTIME_TIMEOUT, "compile_timeout_seconds": COMPILE_TIMEOUT,
        "term_grace_seconds": TERM_GRACE, "phases": list(PHASES),
        "checkouts": {
            "control": {"resolved_path": str(DEFAULT_CONTROL), "head": CONTROL_SHA, "detached": True, "origin": ORIGIN_URL, "status": "", "tree": CONTROL_TREE, "cargo_lock_sha256": CARGO_LOCK_SHA256},
            "pr": {"resolved_path": str(DEFAULT_PR), "head": PR_SHA, "detached": True, "origin": ORIGIN_URL, "status": "", "tree": PR_TREE, "cargo_lock_sha256": CARGO_LOCK_SHA256},
        },
        "target_dirs": {"control": control_target, "pr": pr_target},
        "environment_policy": {"sanitize_before_explicit_env": True, "record_values": "explicit non-secret values only", "stripped_names_recorded": True},
        "prebuild": prebuild, "control": control, "experiment": experiment, "regressions": regressions,
        "conformance": {"historical_source": "targets/salt-multiproof-prove.json", "remove_only": "--test-threads", "entries": conformance},
        "init_cost": {"pairs": 20, "alternating_order": True, "discovery_phase": "init-discovery", "test_threads": 1, "test": BASELINE_TEST, "practical_threshold_relative": 0.05},
    }


def trial_key(phase: str, cohort: str, label: str, trial: int = 1) -> str:
    return f"{CAMPAIGN_ID}/{phase}/{cohort}/{label}/{trial:02d}"


def expected_gate_identities(plan: dict[str, Any]) -> dict[str, dict[str, str]]:
    experiment = {
        trial_key("experiment", "pr", label, index): entry["identity_sha256"]
        for label, entry in plan["experiment"].items() for index in range(1, entry["count"] + 1)
    }
    regressions = {
        trial_key("regressions", "pr", label, index): entry["identity_sha256"]
        for label, entry in plan["regressions"].items() for index in range(1, entry["count"] + 1)
    }
    conformance = {trial_key("conformance", "pr", entry["name"]): entry["identity_sha256"] for entry in plan["conformance"]["entries"]}
    return {"experiment_all_passed": experiment, "regressions_all_passed": regressions, "conformance_all_passed": conformance}


def compute_gates(records: Sequence[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    expected = expected_gate_identities(plan)

    def exact(name: str) -> bool:
        wanted = expected[name]
        relevant = [row for row in records if row.get("trial_key") in wanted]
        keys = [row["trial_key"] for row in relevant]
        return len(keys) == len(wanted) and len(set(keys)) == len(keys) and set(keys) == set(wanted) and all(row.get("status") == "pass" and row.get("identity_sha256") == wanted[row["trial_key"]] for row in relevant)

    observed = bool(records)
    gates: dict[str, Any] = {
        "control_natural_hang_reproduced": any(row.get("phase") == "control" and row.get("timeout") for row in records),
        "experiment_all_passed": exact("experiment_all_passed"),
        "regressions_all_passed": exact("regressions_all_passed"),
        "conformance_all_passed": exact("conformance_all_passed"),
        "no_process_residuals": None if not observed else not any(row.get("residual_pids") for row in records),
    }
    gates["pr148_validation_passed"] = all(gates[name] is True for name in ("experiment_all_passed", "regressions_all_passed", "conformance_all_passed", "no_process_residuals"))
    return gates


def load_or_create_state(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    plan_hash = canonical_hash(plan)
    if path.exists():
        state = json.loads(path.read_text())
        if state.get("campaign_id") != CAMPAIGN_ID or state.get("plan_sha256") != plan_hash:
            raise RuntimeError("state campaign/plan identity mismatch")
        return state
    return {"schema_version": 2, "campaign_id": CAMPAIGN_ID, "plan_sha256": plan_hash, "created_utc": utc_now(), "completed_phases": [], "failed_phases": {}, "active_phase": None, "invocation": 0}


def validate_checkout_snapshot(name: str, expected: dict[str, Any], actual: dict[str, Any]) -> None:
    fields = ("resolved_path", "head", "detached", "origin", "status", "tree", "cargo_lock_sha256")
    differences = {field: {"expected": expected.get(field), "actual": actual.get(field)} for field in fields if expected.get(field) != actual.get(field)}
    if differences:
        raise RuntimeError(f"checkout invariant failed for {name}: {differences}")


def init_cost_statistics(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_pair: dict[int, dict[str, dict[str, Any]]] = {}
    for sample in samples:
        by_pair.setdefault(int(sample["pair"]), {})[sample["cohort"]] = sample
    complete = {pair: rows for pair, rows in by_pair.items() if set(rows) == {"control", "pr"}}
    deltas = [rows["pr"]["seconds"] / rows["control"]["seconds"] - 1.0 for _, rows in sorted(complete.items())]
    strata: dict[str, list[float]] = {"control-first": [], "pr-first": []}
    for _, rows in sorted(complete.items()):
        key = "control-first" if rows["control"]["position"] == 1 else "pr-first"
        strata[key].append(rows["pr"]["seconds"] / rows["control"]["seconds"] - 1.0)
    threshold = 0.05
    median_delta = statistics.median(deltas) if deltas else None
    return {
        "schema_version": 2, "pair_count": len(complete), "practical_threshold_relative": threshold,
        "paired_relative_deltas": deltas,
        "paired_delta_summary": ({"median": median_delta, "min": min(deltas), "max": max(deltas), "p95": percentile95(deltas)} if deltas else None),
        "order_strata": {key: {"n": len(values), "median": statistics.median(values) if values else None} for key, values in strata.items()},
        "practical_interpretation": ("incomplete-descriptive-only" if len(complete) != 20 else ("below-predeclared-5-percent-threshold" if abs(median_delta or 0) < threshold else "at-or-above-predeclared-5-percent-threshold")),
        "disposition": "descriptive paired measurements only; no generic performance authorization",
    }


class Campaign:
    def __init__(self, aro: Path, control: Path, pr: Path, evidence: Path):
        self.aro, self.control, self.pr, self.evidence = (Path(aro).resolve(), Path(control).resolve(), Path(pr).resolve(), Path(evidence).resolve())
        if self.aro != DEFAULT_ARO.resolve() or self.control != DEFAULT_CONTROL.resolve() or self.pr != DEFAULT_PR.resolve():
            raise RuntimeError("campaign requires exact resolved ARO/control/PR paths")
        self.plan = campaign_plan()
        self.targets = {name: self.aro / RUN_ROOT_REL / name for name in ("control", "pr")}
        self.store = EvidenceStore(self.evidence)
        self.cmd = CommandRunner(self.store, [self.control, self.pr, *self.targets.values()])
        self.state_path = self.evidence / "state.json"
        self.state = load_or_create_state(self.state_path, self.plan)
        self.records = self.store.by_trial_key()

    def save_state(self) -> None:
        self.state["updated_utc"] = utc_now()
        atomic_write_json(self.state_path, self.state)

    def _run_entry(self, phase: str, cohort: str, label: str, entry: dict[str, Any], trial: int = 1, *, fail_closed: bool) -> RunRecord | None:
        key = trial_key(phase, cohort, label, trial)
        prior = self.records.get(key)
        if prior:
            if resume_completed(prior, fail_closed=fail_closed, abort_prior_failure=fail_closed):
                return None
            raise RuntimeError(f"non-resumable attempted trial exists: {key}")
        env = {"CARGO_TARGET_DIR": str(self.targets[cohort]), **entry["env"]}
        cwd = self.control if cohort == "control" else self.pr
        record = self.cmd.run(trial_key=key, phase=phase, cohort=cohort, argv=entry["argv"], cwd=cwd, env_delta=env, timeout=entry["timeout_seconds"], context={"variant": label, "trial": trial, "planned_trials": entry["count"]})
        self.records[key] = record.to_dict()
        if fail_closed and record.status != "pass":
            raise RuntimeError(f"fail-closed trial: {key} status={record.status}")
        return record

    def _capture(self, key: str, label: str, argv: list[str], cwd: Path) -> dict[str, Any]:
        prior = self.store.by_trial_key().get(key)
        if prior:
            if prior["status"] != "pass":
                raise RuntimeError(f"prior metadata/check probe blocks resume: {key} status={prior['status']}")
            lines = (self.evidence / prior["log_path"]).read_text(errors="replace").splitlines(keepends=True)
            return {"stdout": "".join(lines[1:]), "record": prior}
        return self.cmd.capture(trial_key=key, phase="metadata", cohort=label, argv=argv, cwd=cwd, timeout=30)

    def checkout_snapshot(self, checkpoint: str) -> dict[str, Any]:
        result = {}
        for name, path in (("control", self.control), ("pr", self.pr)):
            prefix = f"{CAMPAIGN_ID}/checkout/{checkpoint}/{name}"
            commands = {
                "head": ["git", "rev-parse", "HEAD"], "branch": ["git", "symbolic-ref", "-q", "HEAD"],
                "origin": ["git", "remote", "get-url", "origin"],
                "status": ["git", "status", "--porcelain=v2", "--untracked-files=all"],
                "tree": ["git", "rev-parse", "HEAD^{tree}"],
            }
            outputs = {}
            for probe_name, argv in commands.items():
                capture = self._capture(f"{prefix}/{probe_name}", f"checkout-{name}-{probe_name}", argv, path)
                row = capture["record"]
                if probe_name == "branch":
                    if row["exit_code"] not in (0, 1):
                        raise RuntimeError(f"checkout probe failed: {row}")
                elif row["status"] != "pass":
                    raise RuntimeError(f"checkout probe failed: {row}")
                outputs[probe_name] = capture["stdout"].strip()
            actual = {
                "resolved_path": str(path.resolve()), "head": outputs["head"], "detached": outputs["branch"] == "",
                "origin": outputs["origin"], "status": outputs["status"], "tree": outputs["tree"],
                "cargo_lock_sha256": sha256_file(path / "Cargo.lock"),
            }
            validate_checkout_snapshot(name, self.plan["checkouts"][name], actual)
            result[name] = actual
        return result

    def phase_metadata(self) -> None:
        metadata: dict[str, Any] = {"schema_version": 2, "captured_utc": utc_now(), "hostname": socket.gethostname(), "platform": platform.platform(), "python": sys.version, "cpu_count": os.cpu_count(), "commands": {}}
        commands = {"uname": ["uname", "-a"], "lscpu": ["lscpu"], "free": ["free", "-b"], "rustc": ["rustc", "-Vv"], "cargo": ["cargo", "-V"], "rustup": ["rustup", "show"], "git": ["git", "--version"], "gh": ["gh", "--version"]}
        for label, argv in commands.items():
            metadata["commands"][label] = self._capture(trial_key("metadata", "host", label), label, argv, self.aro)
        metadata["commands"]["control-to-pr-diff"] = self._capture(trial_key("metadata", "pr", "control-to-pr-diff"), "control-to-pr-diff", ["git", "diff", "--binary", CONTROL_SHA, PR_SHA], self.pr)
        metadata["commands"]["pr-view"] = self._capture(trial_key("metadata", "host", "pr-view"), "pr-view", ["gh", "pr", "view", "148", "--repo", "megaeth-labs/salt", "--json", "number,url,state,headRefOid,baseRefOid,headRefName,baseRefName,title,author,updatedAt"], self.aro)
        metadata["checkout_invariant"] = self.plan["checkouts"]
        atomic_write_json(self.evidence / "metadata.json", metadata)

    def phase_prebuild(self) -> None:
        for entry in self.plan["prebuild"]:
            self.targets[entry["cohort"]].mkdir(parents=True, exist_ok=True)
            self._run_entry("prebuild", entry["cohort"], entry["name"], entry, fail_closed=True)

    def phase_control(self) -> None:
        for label, entry in self.plan["control"].items():
            for index in range(1, entry["count"] + 1):
                self._run_entry("control", "control", label, entry, index, fail_closed=False)
        rows = [row for row in self.store.existing() if row["phase"] == "control"]
        atomic_write_json(self.evidence / "control-observation.json", {"schema_version": 2, "natural_hang_reproduced": any(row["timeout"] for row in rows), "records": [row["trial_key"] for row in rows]})

    def phase_experiment(self) -> None:
        for label, entry in self.plan["experiment"].items():
            for index in range(1, entry["count"] + 1):
                self._run_entry("experiment", "pr", label, entry, index, fail_closed=True)

    def phase_regressions(self) -> None:
        for label, entry in self.plan["regressions"].items():
            for index in range(1, entry["count"] + 1):
                self._run_entry("regressions", "pr", label, entry, index, fail_closed=True)

    def phase_conformance(self) -> None:
        for entry in self.plan["conformance"]["entries"]:
            self._run_entry("conformance", "pr", entry["name"], entry, fail_closed=True)

    def _lib_test_binary(self, cohort: str) -> Path:
        key = trial_key("prebuild", cohort, "ordinary")
        row = self.store.by_trial_key().get(key)
        if not row or row["status"] != "pass":
            raise RuntimeError(f"canonical ordinary prebuild missing: {key}")
        candidates = set()
        for line in (self.evidence / row["log_path"]).read_text(errors="replace").splitlines()[1:]:
            with contextlib.suppress(json.JSONDecodeError):
                obj = json.loads(line)
                target = obj.get("target", {})
                if obj.get("reason") == "compiler-artifact" and obj.get("executable") and "lib" in target.get("kind", []) and target.get("name") == "salt" and obj.get("profile", {}).get("test"):
                    candidates.add(Path(obj["executable"]))
        existing = sorted(path for path in candidates if path.is_file())
        if len(existing) != 1:
            raise RuntimeError(f"expected one canonical salt lib test binary for {cohort}, got {existing}")
        return existing[0]

    def _resolve_test(self, cohort: str, binary: Path) -> str:
        key = trial_key("init-discovery", cohort, "list")
        prior = self.store.by_trial_key().get(key)
        if prior:
            if prior["status"] != "pass":
                raise RuntimeError(f"prior list discovery failed: {key}")
            row = prior
        else:
            captured = self.cmd.capture(trial_key=key, phase="init-discovery", cohort=cohort, argv=[str(binary), "--list"], cwd=self.control if cohort == "control" else self.pr, env_delta={"CARGO_TARGET_DIR": str(self.targets[cohort])}, timeout=RUNTIME_TIMEOUT)
            row = captured["record"]
        lines = (self.evidence / row["log_path"]).read_text(errors="replace").splitlines()[1:]
        matches = [line.split(": test", 1)[0].strip() for line in lines if line.endswith(": test") and line.split(": test", 1)[0].strip() == BASELINE_TEST]
        if len(matches) != 1:
            raise RuntimeError(f"baseline test resolution failed for {cohort}: {matches}")
        return matches[0]

    def phase_init_cost(self) -> None:
        binaries = {cohort: self._lib_test_binary(cohort) for cohort in ("control", "pr")}
        tests = {cohort: self._resolve_test(cohort, binaries[cohort]) for cohort in ("control", "pr")}
        samples = []
        for pair in range(1, 21):
            order = ("control", "pr") if pair % 2 else ("pr", "control")
            for position, cohort in enumerate(order, 1):
                key = trial_key("init-cost", cohort, f"pair-{pair:02d}-pos-{position}")
                prior = self.store.by_trial_key().get(key)
                if prior:
                    if not resume_completed(prior, fail_closed=True, abort_prior_failure=True):
                        raise RuntimeError(f"invalid prior init-cost trial: {key}")
                    row = prior
                else:
                    record = self.cmd.run(trial_key=key, phase="init-cost", cohort=cohort, argv=[str(binaries[cohort]), tests[cohort], "--exact", "--test-threads=1"], cwd=self.control if cohort == "control" else self.pr, env_delta={"CARGO_TARGET_DIR": str(self.targets[cohort])}, timeout=RUNTIME_TIMEOUT, context={"pair": pair, "position": position, "dedicated_micro_measurement": True})
                    if record.status != "pass":
                        raise RuntimeError(f"init-cost fail-closed: {key}")
                    row = record.to_dict()
                samples.append({"pair": pair, "position": position, "cohort": cohort, "seconds": row["process_elapsed_seconds"], "trial_key": key})
        atomic_write_json(self.evidence / "init-cost-summary.json", init_cost_statistics(samples))

    def phase_finalize(self) -> None:
        generate_outputs(self.evidence, self.state)

    def run(self, selected: Sequence[str]) -> None:
        selected_set = set(selected)
        if unknown := selected_set - set(PHASES):
            raise ValueError(f"unknown phases: {sorted(unknown)}")
        completed = set(self.state["completed_phases"])
        self.state["invocation"] += 1
        invocation = self.state["invocation"]
        self.save_state()
        self.checkout_snapshot(f"invoke-{invocation:03d}-before-first-command")
        methods = {phase: getattr(self, f"phase_{phase.replace('-', '_')}") for phase in PHASES}
        for phase in PHASES:
            if phase not in selected_set:
                continue
            prereqs = set(PHASES[: PHASES.index(phase)]) - {"finalize"}
            if phase != "metadata" and not prereqs.issubset(completed | selected_set):
                raise RuntimeError(f"phase {phase} prerequisites missing: {sorted(prereqs - completed - selected_set)}")
            if phase in completed:
                continue
            self.state["active_phase"] = phase
            self.save_state()
            phase_error: BaseException | None = None
            try:
                methods[phase]()
            except BaseException as exc:
                phase_error = exc
            try:
                self.checkout_snapshot(f"invoke-{invocation:03d}-after-{phase}")
            except BaseException as invariant_exc:
                phase_error = invariant_exc
            if phase_error is not None:
                self.state["failed_phases"][phase] = {"utc": utc_now(), "error": repr(phase_error)}
                self.state["active_phase"] = None
                self.save_state()
                generate_outputs(self.evidence, self.state)
                raise phase_error
            self.state["failed_phases"].pop(phase, None)
            self.state["completed_phases"].append(phase)
            completed.add(phase)
            self.state["active_phase"] = None
            self.save_state()
        generate_outputs(self.evidence, self.state)


def generate_outputs(evidence: Path, state: dict[str, Any] | None = None) -> None:
    store = EvidenceStore(evidence)
    store.rebuild_jsonl()
    records = store.existing()
    statuses: dict[str, int] = {}
    phases: dict[str, dict[str, int]] = {}
    groups: dict[str, dict[str, Any]] = {}
    for row in records:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        phases.setdefault(row["phase"], {})[row["status"]] = phases.setdefault(row["phase"], {}).get(row["status"], 0) + 1
        key = f"{row['phase']}/{row.get('context', {}).get('variant') or row['cohort']}"
        group = groups.setdefault(key, {"runs": 0, "pass": 0, "timeout": 0, "fail": 0, "process_elapsed_seconds": []})
        group["runs"] += 1
        group["pass"] += row["status"] == "pass"
        group["timeout"] += bool(row["timeout"])
        group["fail"] += row["status"] not in {"pass", "timeout"}
        if row["status"] == "pass":
            group["process_elapsed_seconds"].append(row["process_elapsed_seconds"])
    for group in groups.values():
        values = group.pop("process_elapsed_seconds")
        group["pass_process_elapsed_seconds"] = ({"min": min(values), "median": statistics.median(values), "p95": percentile95(values), "max": max(values)} if values else None)
    plan = campaign_plan()
    gates = compute_gates(records, plan)
    if state is None and (evidence / "state.json").exists():
        state = json.loads((evidence / "state.json").read_text())
    summary = {"schema_version": 2, "generated_utc": utc_now(), "campaign_started": bool(records), "run_count": len(records), "statuses": statuses, "phases": phases, "groups": groups, "gates": gates, "state": state or {}, "plan_sha256": canonical_hash(plan)}
    atomic_write_json(evidence / "summary.json", summary)
    lines = ["# Salt PR #148 livelock verification report", "", f"Generated: `{summary['generated_utc']}`", "", f"Attempted subprocess records: **{len(records)}**", "", "## Gates", ""]
    lines += [f"- `{key}`: **{'null' if value is None else str(value).lower()}**" for key, value in sorted(gates.items())]
    lines += ["", "## Exact groups", ""]
    lines += [f"- `{key}`: {row['pass']}/{row['runs']} pass; {row['timeout']} timeout; {row['fail']} other failure" for key, row in sorted(groups.items())]
    lines += ["", "## State", "", "```json", json.dumps(state or {}, indent=2, sort_keys=True), "```", "", "## Init cost", "", "Paired descriptive statistics use process elapsed only, stratify alternating order, and compare against the predeclared 5% practical threshold. They never emit a generic performance-claim authorization.", ""]
    atomic_write_text(evidence / "REPORT.md", "\n".join(lines))
    refresh_manifest(evidence)


def refresh_manifest(evidence: Path) -> None:
    files = []
    for path in sorted(item for item in evidence.rglob("*") if item.is_file() and "__pycache__" not in item.parts and item.name not in {"manifest.json", ".campaign.lock"} and ".tmp" not in item.name):
        files.append({"path": str(path.relative_to(evidence)), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    files.append({"path": "manifest.json", "bytes": None, "sha256": None, "note": "self-entry intentionally unhashed"})
    atomic_write_json(evidence / "manifest.json", {"schema_version": 2, "generated_utc": utc_now(), "files": files})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aro", type=Path, default=DEFAULT_ARO)
    parser.add_argument("--control", type=Path, default=DEFAULT_CONTROL)
    parser.add_argument("--pr", type=Path, default=DEFAULT_PR)
    parser.add_argument("--evidence", type=Path, default=HERE)
    parser.add_argument("--run", action="store_true", help="explicitly authorize campaign execution")
    parser.add_argument("--phase", action="append", choices=PHASES, help="phase(s), canonical order; default all")
    parser.add_argument("--describe-plan", action="store_true")
    parser.add_argument("--refresh-manifest", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not (args.run or args.describe_plan or args.refresh_manifest):
        print("refusing to start campaign without explicit --run", file=sys.stderr)
        return 2
    with CampaignLock(args.evidence / ".campaign.lock"):
        if args.describe_plan:
            atomic_write_json(args.evidence / "campaign-plan.json", campaign_plan())
        if args.refresh_manifest:
            refresh_manifest(args.evidence)
        if args.run:
            for path in (args.aro, args.control, args.pr):
                if not path.is_dir():
                    raise SystemExit(f"required directory missing: {path}")
            Campaign(args.aro, args.control, args.pr, args.evidence).run(args.phase or list(PHASES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
