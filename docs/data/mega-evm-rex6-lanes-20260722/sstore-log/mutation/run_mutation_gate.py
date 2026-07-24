#!/usr/bin/env python3
"""Lane1 mutation-sensitivity gate on exact REX6 baseline."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ARO_ROOT = Path("/nvme2/mega-engineer/workspace/aro")
SOURCE_REPO = Path("/home/mega-engineer/workspace/mega-evm-aro")
BASELINE = "245476834741de1e1a615d22e6287621b64f30cb"
EXPECTED_DIFF = "DIFF 6f26a41c0c58774723597fb0e1e58c07bb7e8bf5b3087b3f8aa293a10c00ec21\n"
TIMED_PROBE = ARO_ROOT / "probes/mega_evm_rex6_sstore_log.rs"
DIFF_PROBE = ARO_ROOT / "probes/mega_evm_rex6_sstore_log_diff.rs"
OUT = Path(__file__).resolve().parent
EDITABLE = [
    "crates/mega-evm/src/evm/host.rs",
    "crates/mega-evm/src/evm/instructions.rs",
    "crates/mega-evm/src/external/gas.rs",
    "crates/mega-evm/src/limit/compute_gas.rs",
    "crates/mega-evm/src/limit/data_size.rs",
    "crates/mega-evm/src/limit/frame_limit.rs",
    "crates/mega-evm/src/limit/kv_update.rs",
    "crates/mega-evm/src/limit/limit.rs",
    "crates/mega-evm/src/limit/state_growth.rs",
]

sys.path.insert(0, str(ARO_ROOT))
from aro.spec import Goal, Stop, TargetSpec  # noqa: E402
from aro.target import SpecTarget  # noqa: E402


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def run(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def make_spec(repo: Path) -> TargetSpec:
    return TargetSpec(
        name="rex6-lane1-mutation",
        repo=repo,
        baseline_ref=BASELINE,
        build=[],
        test=[],
        goal=Goal(metric="ns_per_call", direction="minimize"),
        stop=Stop(),
        probe={
            "pkg": "mega-evm",
            "probe": str(TIMED_PROBE),
            "example": "aro_rex6_lane1_sstore_log",
            "sample_prefix": "BENCH",
            "cargo_args": [],
        },
        differential={
            "pkg": "mega-evm",
            "probe": str(DIFF_PROBE),
            "example": "aro_rex6_lane1_sstore_log_diff",
            "prefix": "DIFF",
        },
        editable=list(EDITABLE),
        probe_covers=list(EDITABLE),
    )


def apply_semantic(worktree: Path) -> dict:
    # Corrupt SSTORE_SET gas formula inside editable external/gas.rs.
    # gas_used is part of the DIFF fingerprint → digest must move.
    path = worktree / "crates/mega-evm/src/external/gas.rs"
    text = path.read_text()
    old = """    fn sstore_set_gas_for_multiplier(&self, multiplier: u64) -> u64 {
        if self.spec.is_enabled(MegaSpecId::REX) {
            constants::rex::SSTORE_SET_STORAGE_GAS_BASE.saturating_mul(multiplier - 1)
        } else {
            constants::mini_rex::SSTORE_SET_STORAGE_GAS.saturating_mul(multiplier)
        }
    }"""
    new = """    fn sstore_set_gas_for_multiplier(&self, multiplier: u64) -> u64 {
        // LANE1_SEMANTIC_MUTATION: inflate storage gas by 1 so gas_used changes.
        let base = if self.spec.is_enabled(MegaSpecId::REX) {
            constants::rex::SSTORE_SET_STORAGE_GAS_BASE.saturating_mul(multiplier - 1)
        } else {
            constants::mini_rex::SSTORE_SET_STORAGE_GAS.saturating_mul(multiplier)
        };
        base.saturating_add(1)
    }"""
    if old not in text:
        raise RuntimeError("semantic gas anchor not found")
    path.write_text(text.replace(old, new, 1))
    return {"file": str(path.relative_to(worktree)), "kind": "semantic", "marker": "LANE1_SEMANTIC_MUTATION"}


def apply_perf(worktree: Path) -> dict:
    # Semantics-preserving burn in inspect_storage REX4 path (editable host.rs).
    path = worktree / "crates/mega-evm/src/evm/host.rs"
    text = path.read_text()
    anchor = "        let transaction_id = self.transaction_id;\n        let is_rex4_enabled = spec.is_enabled(MegaSpecId::REX4);"
    if anchor not in text:
        raise RuntimeError("perf anchor not found in host.rs inspect_storage")
    inject = (
        "        let transaction_id = self.transaction_id;\n"
        "        let is_rex4_enabled = spec.is_enabled(MegaSpecId::REX4);\n"
        "        // LANE1_PERF_MUTATION: pure burn, no observable state change.\n"
        "        {\n"
        "            let mut acc: u64 = transaction_id as u64;\n"
        "            for i in 0..50_000u64 {\n"
        "                acc = acc.wrapping_mul(1664525).wrapping_add(1013904223).wrapping_add(i);\n"
        "                core::hint::black_box(acc);\n"
        "            }\n"
        "        }"
    )
    text = text.replace(anchor, inject, 1)
    path.write_text(text)
    return {"file": str(path.relative_to(worktree)), "kind": "perf", "marker": "LANE1_PERF_MUTATION", "iters": 50000}


def cleanup(source: Path, worktree: Path, target_dir: Path):
    errs = []
    try:
        reg = run(["git", "-C", str(source), "worktree", "list", "--porcelain"]).stdout
        if str(worktree) in reg:
            run(["git", "-C", str(source), "worktree", "remove", "--force", str(worktree)])
    except Exception as e:
        errs.append(str(e))
    for p in (worktree, target_dir):
        try:
            if p.exists():
                shutil.rmtree(p)
        except Exception as e:
            errs.append(str(e))
    try:
        run(["git", "-C", str(source), "worktree", "prune"])
    except Exception as e:
        errs.append(str(e))
    if worktree.exists() or target_dir.exists():
        errs.append("paths survived cleanup")
    if errs:
        raise RuntimeError("; ".join(errs))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    token = f"lane1-mut-{int(time.time())}-{os.getpid()}"
    reservation = Path(tempfile.mkdtemp(prefix="lane1-owned-", dir="/nvme2/mega-engineer/workspace"))
    worktree = reservation / "worktree"
    target_dir = Path(f"/nvme2/mega-engineer/workspace/.aro-{token}-td")
    report = {
        "baseline": BASELINE,
        "expected_diff": EXPECTED_DIFF.strip(),
        "editable": EDITABLE,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    original = None
    try:
        run(["git", "-C", str(SOURCE_REPO), "worktree", "add", "--detach", str(worktree), BASELINE])
        run(["git", "-C", str(worktree), "submodule", "update", "--init", "--recursive"])
        head = run(["git", "-C", str(worktree), "rev-parse", "HEAD"]).stdout.strip()
        assert head == BASELINE, head
        baseline_host = sha256_file(worktree / "crates/mega-evm/src/evm/host.rs")
        baseline_gas = sha256_file(worktree / "crates/mega-evm/src/external/gas.rs")
        report["baseline_file_sha256"] = {
            "host.rs": baseline_host,
            "gas.rs": baseline_gas,
        }

        # SpecTarget reserved for future; cargo examples injected directly.

        # --- restore baseline DIFF ---
        # Inject probes as cargo examples (no SpecTarget.write_probe dependency).
        ex = worktree / "crates/mega-evm/examples"
        ex.mkdir(parents=True, exist_ok=True)
        (ex / "aro_rex6_lane1_sstore_log.rs").write_text(TIMED_PROBE.read_text())
        (ex / "aro_rex6_lane1_sstore_log_diff.rs").write_text(DIFF_PROBE.read_text())

        def run_diff() -> str:
            # Build+run differential example
            out = run(
                [
                    "cargo",
                    "run",
                    "--release",
                    "-p",
                    "mega-evm",
                    "--example",
                    "aro_rex6_lane1_sstore_log_diff",
                ],
                cwd=worktree,
            )
            # cargo run prints compile noise on stderr; stdout should contain DIFF
            lines = [ln for ln in out.stdout.splitlines() if ln.startswith("DIFF ")]
            if not lines:
                raise RuntimeError(f"no DIFF in stdout: {out.stdout[-500:]} stderr={out.stderr[-500:]}")
            return lines[-1] + "\n"

        def run_timed_ir() -> int:
            # Use callgrind on the timed binary for Ir sensitivity
            bin_dir = worktree / "target/release/examples"
            # ensure built
            run(
                [
                    "cargo",
                    "build",
                    "--release",
                    "-p",
                    "mega-evm",
                    "--example",
                    "aro_rex6_lane1_sstore_log",
                ],
                cwd=worktree,
            )
            binary = bin_dir / "aro_rex6_lane1_sstore_log"
            cg_out = reservation / f"callgrind-{time.time_ns()}.out"
            env = os.environ.copy()
            env["RAYON_NUM_THREADS"] = "1"
            env["ARO_BENCH_SCALE"] = "1"
            cg = subprocess.run(
                [
                    "valgrind",
                    "--tool=callgrind",
                    "--quiet",
                    f"--callgrind-out-file={cg_out}",
                    str(binary),
                ],
                cwd=worktree,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            # parse summary Ir
            ir = None
            for line in cg_out.read_text().splitlines():
                if line.startswith("summary:"):
                    ir = int(line.split()[1])
            if ir is None:
                raise RuntimeError("no summary Ir")
            return ir

        # Build once baseline
        print("building baseline examples...", flush=True)
        run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "mega-evm",
                "--example",
                "aro_rex6_lane1_sstore_log",
                "--example",
                "aro_rex6_lane1_sstore_log_diff",
            ],
            cwd=worktree,
        )

        base_diff = run_diff()
        report["baseline_diff"] = base_diff.strip()
        if base_diff != EXPECTED_DIFF:
            raise RuntimeError(f"baseline DIFF mismatch: {base_diff!r} != {EXPECTED_DIFF!r}")
        (OUT / "baseline.diff").write_text(base_diff)
        print("baseline DIFF OK", base_diff.strip(), flush=True)

        base_ir = run_timed_ir()
        report["baseline_ir"] = base_ir
        print("baseline Ir", base_ir, flush=True)

        # --- semantic mutation ---
        sem = apply_semantic(worktree)
        report["semantic_mutation"] = sem
        (OUT / "semantic.patch").write_text(
            run(["git", "-C", str(worktree), "diff", "--", sem["file"]]).stdout
        )
        print("applied semantic mutation", flush=True)
        # rebuild diff example
        run(
            ["cargo", "build", "--release", "-p", "mega-evm", "--example", "aro_rex6_lane1_sstore_log_diff"],
            cwd=worktree,
        )
        try:
            mut_diff = run_diff()
            report["semantic_diff"] = mut_diff.strip()
            report["semantic_diff_changed"] = mut_diff != EXPECTED_DIFF
        except Exception as e:
            report["semantic_diff"] = None
            report["semantic_diff_error"] = str(e)
            report["semantic_diff_changed"] = True
        (OUT / "semantic.diff").write_text(report.get("semantic_diff") or report.get("semantic_diff_error", ""))
        if not report["semantic_diff_changed"]:
            raise RuntimeError("semantic mutation did not change DIFF — oracle toothless")
        print("semantic DIFF changed OK", report.get("semantic_diff"), flush=True)

        # restore
        run(["git", "-C", str(worktree), "checkout", "--", "crates/mega-evm/src"])
        assert sha256_file(worktree / "crates/mega-evm/src/external/gas.rs") == baseline_gas
        run(
            ["cargo", "build", "--release", "-p", "mega-evm", "--example", "aro_rex6_lane1_sstore_log_diff"],
            cwd=worktree,
        )
        restored = run_diff()
        report["restored_diff"] = restored.strip()
        report["restored_diff_matches"] = restored == EXPECTED_DIFF
        (OUT / "restored.diff").write_text(restored)
        if restored != EXPECTED_DIFF:
            raise RuntimeError("restore failed to recover DIFF")
        print("restore DIFF OK", flush=True)

        # --- perf mutation ---
        perf = apply_perf(worktree)
        report["perf_mutation"] = perf
        (OUT / "perf.patch").write_text(
            run(["git", "-C", str(worktree), "diff", "--", perf["file"]]).stdout
        )
        run(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "mega-evm",
                "--example",
                "aro_rex6_lane1_sstore_log",
                "--example",
                "aro_rex6_lane1_sstore_log_diff",
            ],
            cwd=worktree,
        )
        perf_diff = run_diff()
        report["perf_diff"] = perf_diff.strip()
        report["perf_diff_unchanged"] = perf_diff == EXPECTED_DIFF
        (OUT / "perf.diff").write_text(perf_diff)
        if perf_diff != EXPECTED_DIFF:
            raise RuntimeError("perf mutation changed DIFF — not semantics-preserving")
        perf_ir = run_timed_ir()
        report["perf_ir"] = perf_ir
        report["perf_ir_delta"] = perf_ir - base_ir
        report["perf_ir_detected"] = perf_ir > base_ir * 1.01  # >1% Ir increase
        print("perf Ir", base_ir, "->", perf_ir, "delta", perf_ir - base_ir, flush=True)
        if not report["perf_ir_detected"]:
            raise RuntimeError(f"perf mutation not detected in Ir: {base_ir} -> {perf_ir}")

        # restore again
        run(["git", "-C", str(worktree), "checkout", "--", "crates/mega-evm/src"])
        assert sha256_file(worktree / "crates/mega-evm/src/evm/host.rs") == baseline_host
        report["status"] = "passed"
        report["gate_passed"] = True
        print("MUTATION_GATE_PASS", flush=True)
        return 0
    except Exception as e:
        original = e
        report["status"] = "failed"
        report["gate_passed"] = False
        report["error"] = str(e)
        print("MUTATION_GATE_FAIL", e, flush=True)
        return 1
    finally:
        report["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (OUT / "mutation-gate.json").write_text(json.dumps(report, indent=2) + "\n")
        # sums
        lines = []
        for p in sorted(OUT.iterdir()):
            if p.is_file() and p.name != "SHA256SUMS":
                lines.append(f"{sha256_file(p)}  {p.name}")
        (OUT / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        try:
            cleanup(SOURCE_REPO, worktree, target_dir)
            if reservation.exists():
                shutil.rmtree(reservation)
            report["cleanup_ok"] = True
        except Exception as ce:
            report["cleanup_ok"] = False
            report["cleanup_error"] = str(ce)
            (OUT / "mutation-gate.json").write_text(json.dumps(report, indent=2) + "\n")
        if original is not None:
            # already returning 1
            pass


if __name__ == "__main__":
    raise SystemExit(main())
