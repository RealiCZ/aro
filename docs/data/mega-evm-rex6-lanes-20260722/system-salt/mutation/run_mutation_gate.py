#!/usr/bin/env python3
"""Lane5 mutation-sensitivity gate on exact REX6 baseline."""
from __future__ import annotations
import hashlib, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

ARO_ROOT = Path("/nvme2/mega-engineer/workspace/aro")
SOURCE_REPO = Path("/home/mega-engineer/workspace/mega-evm-aro")
BASELINE = "245476834741de1e1a615d22e6287621b64f30cb"
EXPECTED_DIFF = "DIFF 4f3acb3c8d8581bb19783a8940f3d13b7a53a5b0b5147004534452283757d8ae\n"
TIMED_PROBE = ARO_ROOT / "probes/mega_evm_rex6_system_salt.rs"
DIFF_PROBE = ARO_ROOT / "probes/mega_evm_rex6_system_salt_diff.rs"
OUT = Path(__file__).resolve().parent
EDITABLE = [
    "crates/mega-evm/src/evm/context.rs",
    "crates/mega-evm/src/evm/host.rs",
    "crates/mega-evm/src/evm/instructions.rs",
    "crates/mega-evm/src/external/gas.rs",
    "crates/mega-evm/src/limit/compute_gas.rs",
    "crates/mega-evm/src/limit/frame_limit.rs",
    "crates/mega-evm/src/limit/limit.rs",
    "crates/mega-evm/src/system/tx.rs",
]

def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def run(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)

def apply_semantic(worktree: Path) -> dict:
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
        // LANE5_SEMANTIC_MUTATION
        let base = if self.spec.is_enabled(MegaSpecId::REX) {
            constants::rex::SSTORE_SET_STORAGE_GAS_BASE.saturating_mul(multiplier - 1)
        } else {
            constants::mini_rex::SSTORE_SET_STORAGE_GAS.saturating_mul(multiplier)
        };
        base.saturating_add(1)
    }"""
    if old not in text:
        raise RuntimeError("semantic sstore gas anchor missing")
    path.write_text(text.replace(old, new, 1))
    return {"file": str(path.relative_to(worktree)), "kind": "semantic", "marker": "LANE5_SEMANTIC_MUTATION"}

def apply_perf(worktree: Path) -> dict:
    path = worktree / "crates/mega-evm/src/evm/context.rs"
    text = path.read_text()
    anchor = """    pub(crate) fn on_new_tx(&mut self) {"""
    # need unique with body start
    anchor = """        // REX6+: exempt system-originated transactions (see `crate::is_system_originated`) from
        // MegaETH per-tx resource metering.
        if self.spec.is_enabled(MegaSpecId::REX6) &&
            is_system_originated(&self.inner.tx, self.system_address)
        {
            self.additional_limit.borrow_mut().mark_exempt();
        }"""
    if anchor not in text:
        raise RuntimeError("perf on_new_tx exempt anchor missing")
    inject = """        // LANE5_PERF_MUTATION
        {
            let mut acc: u64 = 1;
            for i in 0..50_000u64 {
                acc = acc.wrapping_mul(1664525).wrapping_add(1013904223).wrapping_add(i);
                core::hint::black_box(acc);
            }
        }
        // REX6+: exempt system-originated transactions (see `crate::is_system_originated`) from
        // MegaETH per-tx resource metering.
        if self.spec.is_enabled(MegaSpecId::REX6) &&
            is_system_originated(&self.inner.tx, self.system_address)
        {
            self.additional_limit.borrow_mut().mark_exempt();
        }"""
    path.write_text(text.replace(anchor, inject, 1))
    return {"file": str(path.relative_to(worktree)), "kind": "perf", "marker": "LANE5_PERF_MUTATION", "iters": 50000}

def cleanup(source, worktree, target_dir, reservation):
    try:
        reg = run(["git","-C",str(source),"worktree","list","--porcelain"]).stdout
        if str(worktree) in reg:
            run(["git","-C",str(source),"worktree","remove","--force",str(worktree)])
    except Exception:
        pass
    for p in (worktree, target_dir, reservation):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    run(["git","-C",str(source),"worktree","prune"], check=False)

def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    token = f"lane5-mut-{int(time.time())}-{os.getpid()}"
    reservation = Path(tempfile.mkdtemp(prefix="lane5-owned-", dir="/nvme2/mega-engineer/workspace"))
    worktree = reservation / "worktree"
    target_dir = Path(f"/nvme2/mega-engineer/workspace/.aro-{token}-td")
    report = {"baseline": BASELINE, "expected_diff": EXPECTED_DIFF.strip(), "editable": EDITABLE,
              "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        run(["git","-C",str(SOURCE_REPO),"worktree","add","--detach",str(worktree),BASELINE])
        run(["git","-C",str(worktree),"submodule","update","--init","--recursive"])
        assert run(["git","-C",str(worktree),"rev-parse","HEAD"]).stdout.strip()==BASELINE
        baseline_ins = sha256_file(worktree/"crates/mega-evm/src/evm/context.rs")
        baseline_gas = sha256_file(worktree/"crates/mega-evm/src/external/gas.rs")
        report["baseline_file_sha256"]={"context.rs":baseline_ins,"gas.rs":baseline_gas}
        ex = worktree/"crates/mega-evm/examples"
        ex.mkdir(parents=True, exist_ok=True)
        (ex/"aro_rex6_lane5_create.rs").write_text(TIMED_PROBE.read_text())
        (ex/"aro_rex6_lane5_create_diff.rs").write_text(DIFF_PROBE.read_text())

        def run_diff() -> str:
            out = run(["cargo","run","--release","-p","mega-evm","--features","test-utils","--example","aro_rex6_lane5_create_diff"], cwd=worktree)
            lines=[ln for ln in out.stdout.splitlines() if ln.startswith("DIFF ")]
            if not lines:
                raise RuntimeError(f"no DIFF: {out.stdout[-400:]} {out.stderr[-400:]}")
            return lines[-1]+"\n"

        def run_timed_ir() -> int:
            run(["cargo","build","--release","-p","mega-evm","--features","test-utils","--example","aro_rex6_lane5_create"], cwd=worktree)
            binary = worktree/"target/release/examples/aro_rex6_lane5_create"
            cg_out = reservation/f"cg-{time.time_ns()}.out"
            env=os.environ.copy(); env["RAYON_NUM_THREADS"]="1"; env["ARO_BENCH_SCALE"]="1"
            subprocess.run(["valgrind","--tool=callgrind","--quiet",f"--callgrind-out-file={cg_out}",str(binary)],
                           cwd=worktree, env=env, text=True, capture_output=True, check=True)
            ir=None
            for line in cg_out.read_text().splitlines():
                if line.startswith("summary:"):
                    ir=int(line.split()[1])
            if ir is None: raise RuntimeError("no summary Ir")
            return ir

        print("building baseline...", flush=True)
        run(["cargo","build","--release","-p","mega-evm","--features","test-utils","--example","aro_rex6_lane5_create","--example","aro_rex6_lane5_create_diff"], cwd=worktree)
        base_diff=run_diff(); report["baseline_diff"]=base_diff.strip()
        if base_diff!=EXPECTED_DIFF: raise RuntimeError(f"baseline DIFF mismatch {base_diff!r}")
        (OUT/"baseline.diff").write_text(base_diff)
        print("baseline DIFF OK", flush=True)
        base_ir=run_timed_ir(); report["baseline_ir"]=base_ir
        print("baseline Ir", base_ir, flush=True)

        sem=apply_semantic(worktree); report["semantic_mutation"]=sem
        (OUT/"semantic.patch").write_text(run(["git","-C",str(worktree),"diff","--",sem["file"]]).stdout)
        run(["cargo","build","--release","-p","mega-evm","--features","test-utils","--example","aro_rex6_lane5_create_diff"], cwd=worktree)
        mut_diff=run_diff(); report["semantic_diff"]=mut_diff.strip()
        report["semantic_diff_changed"]=mut_diff!=EXPECTED_DIFF
        (OUT/"semantic.diff").write_text(mut_diff)
        if not report["semantic_diff_changed"]: raise RuntimeError("semantic mutation did not change DIFF")
        print("semantic OK", mut_diff.strip(), flush=True)

        run(["git","-C",str(worktree),"checkout","--","crates/mega-evm/src"])
        assert sha256_file(worktree/"crates/mega-evm/src/external/gas.rs")==baseline_gas
        run(["cargo","build","--release","-p","mega-evm","--features","test-utils","--example","aro_rex6_lane5_create_diff"], cwd=worktree)
        restored=run_diff(); report["restored_diff"]=restored.strip()
        report["restored_diff_matches"]=restored==EXPECTED_DIFF
        (OUT/"restored.diff").write_text(restored)
        if restored!=EXPECTED_DIFF: raise RuntimeError("restore failed")
        print("restore OK", flush=True)

        perf=apply_perf(worktree); report["perf_mutation"]=perf
        (OUT/"perf.patch").write_text(run(["git","-C",str(worktree),"diff","--",perf["file"]]).stdout)
        run(["cargo","build","--release","-p","mega-evm","--features","test-utils","--example","aro_rex6_lane5_create","--example","aro_rex6_lane5_create_diff"], cwd=worktree)
        perf_diff=run_diff(); report["perf_diff"]=perf_diff.strip()
        report["perf_diff_unchanged"]=perf_diff==EXPECTED_DIFF
        (OUT/"perf.diff").write_text(perf_diff)
        if perf_diff!=EXPECTED_DIFF: raise RuntimeError("perf mutation changed DIFF")
        perf_ir=run_timed_ir(); report["perf_ir"]=perf_ir
        report["perf_ir_delta"]=perf_ir-base_ir
        report["perf_ir_detected"]=perf_ir>base_ir*1.01
        print("perf Ir", base_ir, "->", perf_ir, flush=True)
        if not report["perf_ir_detected"]: raise RuntimeError("perf not detected")
        run(["git","-C",str(worktree),"checkout","--","crates/mega-evm/src"])
        report["status"]="passed"; report["gate_passed"]=True
        print("MUTATION_GATE_PASS", flush=True)
        return 0
    except Exception as e:
        report["status"]="failed"; report["gate_passed"]=False; report["error"]=str(e)
        print("MUTATION_GATE_FAIL", e, flush=True)
        return 1
    finally:
        report["finished_utc"]=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (OUT/"mutation-gate.json").write_text(json.dumps(report, indent=2)+"\n")
        lines=[]
        for f in sorted(OUT.iterdir()):
            if f.is_file() and f.name!="SHA256SUMS":
                lines.append(f"{sha256_file(f)}  {f.name}")
        (OUT/"SHA256SUMS").write_text("\n".join(lines)+"\n")
        cleanup(SOURCE_REPO, worktree, target_dir, reservation)

if __name__ == "__main__":
    raise SystemExit(main())
