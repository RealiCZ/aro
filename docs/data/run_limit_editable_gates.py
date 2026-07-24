#!/usr/bin/env python3
"""Three-gate validation before expanding mega-evm-v2 editable to limit/* packaging files.

Gate1: deterministic fingerprint (DIFF x2 identical)
Gate2: call-trace / dual-reach evidence for limit/{limit,compute_gas,frame_limit}.rs
Gate3: mutation sensitivity on check_limit / record_compute_gas
        - miss_record (漏记)
        - wrong_order (错序: reverse dimension check order in check_limit)
        - wrong_count (错计数: double compute gas record)
Each must turn DIFF red; restore must recover baseline fingerprint.

If stock DIFF is blind, apply enhanced probe that folds MegaTransactionOutcome
4D usage + halt-kind tag via execute_transaction, then re-check.

Never leaves mutations in the target tree. Archives under OUT.
"""
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
STOCK_DIFF_PROBE = ARO_ROOT / "probes/evm_semantics_diff.rs"
TIMED_PROBE = ARO_ROOT / "probes/sweep_hotloop_v2.rs"
OUT = ARO_ROOT / "docs/data/mega-evm-limit-editable-gates-20260724"
PROPOSED_EDITABLE = [
    "crates/mega-evm/src/evm/host.rs",
    "crates/mega-evm/src/evm/instructions.rs",
    "crates/mega-evm/src/limit/limit.rs",
    "crates/mega-evm/src/limit/compute_gas.rs",
    "crates/mega-evm/src/limit/frame_limit.rs",
]
LIMIT_FILES = PROPOSED_EDITABLE[2:]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def run(cmd, cwd=None, check=True, env=None, timeout=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )


def cleanup(source: Path, worktree: Path, *extra: Path):
    errs = []
    try:
        reg = run(["git", "-C", str(source), "worktree", "list", "--porcelain"]).stdout
        if str(worktree) in reg:
            run(
                ["git", "-C", str(source), "worktree", "remove", "--force", str(worktree)],
                check=False,
            )
    except Exception as e:
        errs.append(str(e))
    for p in (worktree, *extra):
        try:
            if p.exists():
                shutil.rmtree(p)
        except Exception as e:
            errs.append(str(e))
    try:
        run(["git", "-C", str(source), "worktree", "prune"], check=False)
    except Exception as e:
        errs.append(str(e))
    return errs


def enhance_diff_probe(src: str) -> str:
    """Patch stock DIFF to fold 4D LimitUsage + halt tag via execute_transaction."""
    if "LIMIT_EDITABLE_ENHANCED_DIFF_V1" in src:
        return src

    old_imp = """use mega_evm::{
    constants::{
        mini_rex::ORACLE_ACCESS_COMPUTE_GAS as MINI_REX_ORACLE_DETENTION,
        rex::TX_COMPUTE_GAS_LIMIT as REX_TX_COMPUTE_GAS_LIMIT,
        rex3::ORACLE_ACCESS_COMPUTE_GAS as REX3_ORACLE_DETENTION,
        rex4::STORAGE_CALL_STIPEND,
    },
    IMegaAccessControl, IMegaLimitControl, MegaContext, MegaEvm, MegaSpecId, MegaTransaction,
    ACCESS_CONTROL_ADDRESS, LIMIT_CONTROL_ADDRESS, ORACLE_CONTRACT_ADDRESS,
};"""
    new_imp = """use mega_evm::{
    constants::{
        mini_rex::ORACLE_ACCESS_COMPUTE_GAS as MINI_REX_ORACLE_DETENTION,
        rex::TX_COMPUTE_GAS_LIMIT as REX_TX_COMPUTE_GAS_LIMIT,
        rex3::ORACLE_ACCESS_COMPUTE_GAS as REX3_ORACLE_DETENTION,
        rex4::STORAGE_CALL_STIPEND,
    },
    IMegaAccessControl, IMegaLimitControl, MegaContext, MegaEvm, MegaHaltReason, MegaSpecId,
    MegaTransaction, ACCESS_CONTROL_ADDRESS, LIMIT_CONTROL_ADDRESS, ORACLE_CONTRACT_ADDRESS,
};
// LIMIT_EDITABLE_ENHANCED_DIFF_V1
use revm::context::result::ExecutionResult;"""
    if old_imp not in src:
        raise RuntimeError("import block anchor missing for enhanced DIFF")
    src = src.replace(old_imp, new_imp, 1)

    old_outcome = """/// Outcome folded into the fingerprint for one execution.
struct Outcome {
    success: bool,
    gas: u64,
    output: Vec<u8>,
    slots: [U256; 6],
}

fn fold_outcome(fp: u64, o: &Outcome) -> u64 {
    let mut fp = fnv1a(fp, &[o.success as u8]);
    fp = fnv1a(fp, &o.gas.to_le_bytes());
    fp = fnv1a(fp, &(o.output.len() as u64).to_le_bytes());
    fp = fnv1a(fp, &o.output);
    for s in &o.slots {
        fp = fnv1a(fp, &s.to_le_bytes::<32>());
    }
    fp
}"""
    new_outcome = """/// Outcome folded into the fingerprint for one execution.
/// Enhanced (LIMIT_EDITABLE_ENHANCED_DIFF_V1): also folds 4D LimitUsage + halt kind tag
/// so miss-record / wrong-order / wrong-count on AdditionalLimit turn DIFF red.
struct Outcome {
    success: bool,
    gas: u64,
    output: Vec<u8>,
    slots: [U256; 6],
    halt_tag: u8,
    data_size: u64,
    kv_updates: u64,
    compute_gas: u64,
    state_growth: u64,
}

fn fold_outcome(fp: u64, o: &Outcome) -> u64 {
    let mut fp = fnv1a(fp, &[o.success as u8]);
    fp = fnv1a(fp, &o.gas.to_le_bytes());
    fp = fnv1a(fp, &(o.output.len() as u64).to_le_bytes());
    fp = fnv1a(fp, &o.output);
    for s in &o.slots {
        fp = fnv1a(fp, &s.to_le_bytes::<32>());
    }
    fp = fnv1a(fp, &[o.halt_tag]);
    fp = fnv1a(fp, &o.data_size.to_le_bytes());
    fp = fnv1a(fp, &o.kv_updates.to_le_bytes());
    fp = fnv1a(fp, &o.compute_gas.to_le_bytes());
    fp = fnv1a(fp, &o.state_growth.to_le_bytes());
    fp
}

fn halt_tag(reason: &MegaHaltReason) -> u8 {
    match reason {
        MegaHaltReason::Base(_) => 1,
        MegaHaltReason::DataLimitExceeded { .. } => 2,
        MegaHaltReason::KVUpdateLimitExceeded { .. } => 3,
        MegaHaltReason::ComputeGasLimitExceeded { .. } => 4,
        MegaHaltReason::StateGrowthLimitExceeded { .. } => 5,
        MegaHaltReason::SystemTxInvalidCallee { .. } => 6,
        MegaHaltReason::VolatileDataAccessOutOfGas { .. } => 7,
    }
}"""
    if old_outcome not in src:
        raise RuntimeError("Outcome block anchor missing")
    src = src.replace(old_outcome, new_outcome, 1)

    old_run = """    let res = alloy_evm::Evm::transact_raw(&mut evm, tx);
    match res {
        Ok(r) => {
            let success = r.result.is_success();
            let gas = r.result.gas_used();
            let output = r.result.output().cloned().unwrap_or_default().to_vec();
            let mut slots = [U256::ZERO; 6];
            if let Some(addr) = read_slots_from {
                for slot in 0u8..6 {
                    slots[slot as usize] = r
                        .state
                        .get(&addr)
                        .and_then(|acc| acc.storage.get(&U256::from(slot)))
                        .map(|s| s.present_value)
                        .unwrap_or(U256::ZERO);
                }
            }
            Outcome { success, gas, output, slots }
        }
        Err(_) => Outcome {
            success: false,
            gas: u64::MAX,
            output: Vec::new(),
            slots: [U256::MAX; 6],
        },
    }
}"""
    new_run = """    // Enhanced: public MegaEvm::execute_transaction exposes 4D LimitUsage + MegaHaltReason.
    let res = evm.execute_transaction(tx);
    match res {
        Ok(r) => {
            let success = r.result.is_success();
            let gas = r.result.gas_used();
            let output = r.result.output().cloned().unwrap_or_default().to_vec();
            let mut slots = [U256::ZERO; 6];
            if let Some(addr) = read_slots_from {
                for slot in 0u8..6 {
                    slots[slot as usize] = r
                        .state
                        .get(&addr)
                        .and_then(|acc| acc.storage.get(&U256::from(slot)))
                        .map(|s| s.present_value)
                        .unwrap_or(U256::ZERO);
                }
            }
            let tag = match &r.result {
                ExecutionResult::Success { .. } => 0u8,
                ExecutionResult::Revert { .. } => 8u8,
                ExecutionResult::Halt { reason, .. } => halt_tag(reason),
            };
            Outcome {
                success,
                gas,
                output,
                slots,
                halt_tag: tag,
                data_size: r.data_size,
                kv_updates: r.kv_updates,
                compute_gas: r.compute_gas_used,
                state_growth: r.state_growth_used,
            }
        }
        Err(_) => Outcome {
            success: false,
            gas: u64::MAX,
            output: Vec::new(),
            slots: [U256::MAX; 6],
            halt_tag: 255,
            data_size: u64::MAX,
            kv_updates: u64::MAX,
            compute_gas: u64::MAX,
            state_growth: u64::MAX,
        },
    }
}"""
    if old_run not in src:
        raise RuntimeError("run_tx result block anchor missing")
    src = src.replace(old_run, new_run, 1)

    src = src.replace(
        "//! Spec matrix: MINI_REX, REX, REX3, REX4, REX5. Folds success, gas_used, returndata,\n//! and read-back storage into one FNV-1a fingerprint printed as `DIFF <hex>`.",
        "//! Spec matrix: MINI_REX, REX, REX3, REX4, REX5. Folds success, gas_used, returndata,\n//! read-back storage, halt-kind tag, and 4D LimitUsage (data/kv/compute/state_growth)\n//! into one FNV-1a fingerprint printed as `DIFF <hex>`.\n//! LIMIT_EDITABLE_ENHANCED_DIFF_V1",
        1,
    )
    return src


MUTATIONS = {
    "miss_record": {
        "file": "crates/mega-evm/src/limit/limit.rs",
        "desc": "漏记: skip compute_gas.record_gas_used in record_compute_gas",
        "old": """    pub(crate) fn record_compute_gas(&mut self, compute_gas_used: u64) -> bool {
        // Record unconditionally, even when another dimension has already latched an exceed:
        // the compute work was performed, and the recorded total feeds the transaction outcome
        // and block-level compute accounting. Skipping the record would under-report compute
        // usage for transactions halted on a non-compute dimension (e.g. intrinsic data size
        // latched in `before_tx_start` before `validate` records the initial gas).
        self.compute_gas.record_gas_used(compute_gas_used);""",
        "new": """    pub(crate) fn record_compute_gas(&mut self, compute_gas_used: u64) -> bool {
        // MUTATION miss_record: deliberately skip recording compute gas.
        let _ = compute_gas_used;
        // self.compute_gas.record_gas_used(compute_gas_used);""",
    },
    "wrong_order": {
        "file": "crates/mega-evm/src/limit/limit.rs",
        "desc": "错序: reverse sub-tracker check order in check_limit (state→compute→kv→data)",
        "old": """        let data_size_check = self.data_size.check_limit();
        if data_size_check.exceeded_limit() {
            self.has_exceeded_limit = data_size_check;
            return self.has_exceeded_limit;
        }

        let kv_update_check = self.kv_update.check_limit();
        if kv_update_check.exceeded_limit() {
            self.has_exceeded_limit = kv_update_check;
            return self.has_exceeded_limit;
        }

        // Per-frame compute gas check (Rex4+) and TX-level detained check (all specs).
        let compute_gas_check = self.compute_gas.check_limit();
        if compute_gas_check.exceeded_limit() {
            self.has_exceeded_limit = compute_gas_check;
            return self.has_exceeded_limit;
        }

        // State growth check:
        // - Rex4+: frame-local budget check.
        // - pre-Rex4: TX-level check inside `state_growth.check_limit()`.
        let state_growth_check = self.state_growth.check_limit();
        if state_growth_check.exceeded_limit() {
            self.has_exceeded_limit = state_growth_check;
            return self.has_exceeded_limit;
        }

        self.has_exceeded_limit
    }""",
        "new": """        // MUTATION wrong_order: reverse dimension priority vs production Resource-Limit Check Protocol.
        let state_growth_check = self.state_growth.check_limit();
        if state_growth_check.exceeded_limit() {
            self.has_exceeded_limit = state_growth_check;
            return self.has_exceeded_limit;
        }

        let compute_gas_check = self.compute_gas.check_limit();
        if compute_gas_check.exceeded_limit() {
            self.has_exceeded_limit = compute_gas_check;
            return self.has_exceeded_limit;
        }

        let kv_update_check = self.kv_update.check_limit();
        if kv_update_check.exceeded_limit() {
            self.has_exceeded_limit = kv_update_check;
            return self.has_exceeded_limit;
        }

        let data_size_check = self.data_size.check_limit();
        if data_size_check.exceeded_limit() {
            self.has_exceeded_limit = data_size_check;
            return self.has_exceeded_limit;
        }

        self.has_exceeded_limit
    }""",
    },
    "wrong_count": {
        "file": "crates/mega-evm/src/limit/limit.rs",
        "desc": "错计数: double-count compute gas on every record_compute_gas",
        "old": """        self.compute_gas.record_gas_used(compute_gas_used);
        // Sticky short-circuit, mirroring `check_limit`: an already-latched `ExceedsLimit` is""",
        "new": """        // MUTATION wrong_count: record twice (2x compute usage).
        self.compute_gas.record_gas_used(compute_gas_used);
        self.compute_gas.record_gas_used(compute_gas_used);
        // Sticky short-circuit, mirroring `check_limit`: an already-latched `ExceedsLimit` is""",
    },
}


def apply_mutation(worktree: Path, key: str) -> dict:
    m = MUTATIONS[key]
    path = worktree / m["file"]
    text = path.read_text()
    if m["old"] not in text:
        raise RuntimeError(f"{key}: anchor not found in {m['file']}")
    path.write_text(text.replace(m["old"], m["new"], 1))
    return {"key": key, "file": m["file"], "desc": m["desc"], "sha_after": sha256_file(path)}


def restore_file(worktree: Path, rel: str, baseline_text: str):
    path = worktree / rel
    path.write_text(baseline_text)
    assert path.read_text() == baseline_text


def parse_symbol_report(path: Path) -> dict:
    hits = {}
    if not path.exists():
        return hits
    for line in path.read_text(errors="replace").splitlines():
        low = line.lower()
        if any(
            k in low
            for k in (
                "check_limit",
                "record_compute",
                "additional_limit",
                "compute_gas",
                "frame_limit",
                "limit.rs",
            )
        ):
            hits[line.strip()] = True
    return hits


def gate2_calltrace_evidence(report: dict) -> dict:
    base = ARO_ROOT / "docs/data/mega-evm-hwcounters-20260723"
    evidence = {
        "timed_probe": "probes/sweep_hotloop_v2.rs",
        "diff_probe": "probes/evm_semantics_diff.rs",
        "proposed_editable": PROPOSED_EDITABLE,
        "sources": {},
        "limit_symbols_seen": [],
        "files_named": [],
    }
    for name in [
        "record/sweep_hotloop_v2_cycles/report.symbol.txt",
        "record/sweep_hotloop_v2_instructions/report.symbol.txt",
        "record/aro_rex6_lane1_sstore_log_cycles/report.symbol.txt",
    ]:
        p = base / name
        hits = parse_symbol_report(p)
        evidence["sources"][name] = {
            "exists": p.exists(),
            "hit_lines": list(hits)[:40],
            "n": len(hits),
        }
        evidence["limit_symbols_seen"].extend(list(hits)[:20])

    t2a = ARO_ROOT / "docs/mega-evm-t2a-push1-wrapper-decompose-20260724.md"
    if t2a.exists():
        t = t2a.read_text()
        for f in LIMIT_FILES:
            if Path(f).name in t or "check_limit" in t or "record_compute_gas" in t:
                evidence["files_named"].append(f)

    diff_src = STOCK_DIFF_PROBE.read_text()
    evidence["diff_has_compute_heavy"] = "run_compute_heavy" in diff_src
    evidence["diff_has_data_heavy"] = "run_data_heavy" in diff_src
    evidence["timed_is_sweep"] = TIMED_PROBE.exists()

    symbol_ok = any(s.get("n", 0) > 0 for s in evidence["sources"].values())
    corpus_ok = evidence["diff_has_compute_heavy"] and evidence["diff_has_data_heavy"]
    evidence["gate2_pass"] = bool(symbol_ok and corpus_ok)
    evidence["gate2_notes"] = (
        "Intersection policy: host+instructions+limit/{limit,compute_gas,frame_limit} "
        "for packaging mine. Other limit/* trackers remain out of editable until separate gates."
    )
    report["gate2"] = evidence
    return evidence


def write_enhanced_probe_to_aro(enhanced_text: str) -> str:
    dest = ARO_ROOT / "probes/evm_semantics_diff.rs"
    backup = OUT / "evm_semantics_diff.stock.bak.rs"
    if not backup.exists():
        backup.write_text(STOCK_DIFF_PROBE.read_text())
    dest.write_text(enhanced_text)
    return sha256_file(dest)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {
        "baseline": BASELINE,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "proposed_editable": PROPOSED_EDITABLE,
    }
    reservation = Path(
        tempfile.mkdtemp(prefix="limit-gates-", dir="/nvme2/mega-engineer/workspace")
    )
    worktree = reservation / "worktree"
    log_path = OUT / "gate-run.log"

    def log(msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with log_path.open("a") as f:
            f.write(line + "\n")

    try:
        log(f"worktree add {BASELINE}")
        run(
            [
                "git",
                "-C",
                str(SOURCE_REPO),
                "worktree",
                "add",
                "--detach",
                str(worktree),
                BASELINE,
            ]
        )
        run(
            ["git", "-C", str(worktree), "submodule", "update", "--init", "--recursive"],
            check=False,
        )
        head = run(["git", "-C", str(worktree), "rev-parse", "HEAD"]).stdout.strip()
        assert head == BASELINE, head

        lim = worktree / "crates/mega-evm/src/limit/limit.rs"
        cg = worktree / "crates/mega-evm/src/limit/compute_gas.rs"
        fl = worktree / "crates/mega-evm/src/limit/frame_limit.rs"
        for p in (lim, cg, fl):
            assert p.is_file(), p
        baseline_texts = {
            "crates/mega-evm/src/limit/limit.rs": lim.read_text(),
            "crates/mega-evm/src/limit/compute_gas.rs": cg.read_text(),
            "crates/mega-evm/src/limit/frame_limit.rs": fl.read_text(),
        }
        report["baseline_file_sha256"] = {
            k: sha256_bytes(v.encode()) for k, v in baseline_texts.items()
        }

        g2 = gate2_calltrace_evidence(report)
        log(
            f"gate2_pass={g2['gate2_pass']} symbol_hits={[s.get('n') for s in g2['sources'].values()]}"
        )

        ex = worktree / "crates/mega-evm/examples"
        ex.mkdir(parents=True, exist_ok=True)

        def install_probe(text: str, name: str = "evm_semantics_diff"):
            (ex / f"{name}.rs").write_text(text)
            if TIMED_PROBE.exists():
                (ex / "sweep_hotloop_v2.rs").write_text(TIMED_PROBE.read_text())

        stock_text = STOCK_DIFF_PROBE.read_text()
        install_probe(stock_text)

        env = os.environ.copy()
        env["RAYON_NUM_THREADS"] = "1"
        env["CARGO_TERM_COLOR"] = "never"

        def build_diff(example: str = "evm_semantics_diff"):
            log(f"cargo build --release --example {example}")
            r = run(
                ["cargo", "build", "--release", "-p", "mega-evm", "--example", example],
                cwd=worktree,
                env=env,
                timeout=1800,
            )
            if r.returncode != 0:
                raise RuntimeError(f"build failed: {r.stderr[-3000:]}")

        def run_diff(example: str = "evm_semantics_diff") -> str:
            bin_path = worktree / "target/release/examples" / example
            r = run([str(bin_path)], cwd=worktree, env=env, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(
                    f"diff run rc={r.returncode} stderr={r.stderr[-2000:]} stdout={r.stdout[-1000:]}"
                )
            lines = [ln for ln in r.stdout.splitlines() if ln.startswith("DIFF ")]
            if not lines:
                raise RuntimeError(f"no DIFF line: stdout={r.stdout[-1500:]}")
            return lines[-1] + "\n"

        build_diff()
        d1 = run_diff()
        d2 = run_diff()
        report["gate1_stock"] = {
            "diff1": d1.strip(),
            "diff2": d2.strip(),
            "identical": d1 == d2,
            "probe": "stock",
        }
        log(f"gate1 stock identical={d1 == d2} {d1.strip()}")
        if d1 != d2:
            raise RuntimeError("stock DIFF not deterministic")

        stock_baseline = d1
        (OUT / "stock-baseline.diff").write_text(stock_baseline)

        mut_results = {}
        need_enhance = False
        for key in ("miss_record", "wrong_order", "wrong_count"):
            log(f"stock mutation {key}")
            meta = apply_mutation(worktree, key)
            try:
                build_diff()
                md = run_diff()
            except Exception as e:
                md = f"ERROR: {e}"
            changed = isinstance(md, str) and md.startswith("DIFF ") and md != stock_baseline
            mut_results[key] = {
                "with": "stock_diff",
                "meta": meta,
                "diff": md.strip() if isinstance(md, str) else str(md),
                "turned_red": bool(changed),
            }
            log(f"  stock {key} red={changed} diff={mut_results[key]['diff'][:80]}")
            restore_file(worktree, meta["file"], baseline_texts[meta["file"]])
            build_diff()
            back = run_diff()
            mut_results[key]["restored_diff"] = back.strip()
            mut_results[key]["restored_ok"] = back == stock_baseline
            if not changed:
                need_enhance = True

        report["gate3_stock"] = mut_results
        report["stock_needs_enhance"] = need_enhance

        log("enhancing DIFF probe (4D usage + halt tag)")
        enhanced = enhance_diff_probe(stock_text)
        (OUT / "evm_semantics_diff.enhanced.rs").write_text(enhanced)
        install_probe(enhanced)
        build_diff()
        e1 = run_diff()
        e2 = run_diff()
        report["gate1_enhanced"] = {
            "diff1": e1.strip(),
            "diff2": e2.strip(),
            "identical": e1 == e2,
            "probe": "enhanced_v1",
        }
        log(f"gate1 enhanced identical={e1 == e2} {e1.strip()}")
        if e1 != e2:
            raise RuntimeError("enhanced DIFF not deterministic")
        enhanced_baseline = e1
        (OUT / "enhanced-baseline.diff").write_text(enhanced_baseline)

        mut_enh = {}
        for key in ("miss_record", "wrong_order", "wrong_count"):
            log(f"enhanced mutation {key}")
            meta = apply_mutation(worktree, key)
            try:
                build_diff()
                md = run_diff()
            except Exception as e:
                md = f"ERROR: {e}"
            changed = isinstance(md, str) and md.startswith("DIFF ") and md != enhanced_baseline
            mut_enh[key] = {
                "with": "enhanced_diff",
                "meta": meta,
                "diff": md.strip() if isinstance(md, str) else str(md),
                "turned_red": bool(changed),
            }
            log(f"  enh {key} red={changed} diff={mut_enh[key]['diff'][:80]}")
            restore_file(worktree, meta["file"], baseline_texts[meta["file"]])
            build_diff()
            back = run_diff()
            mut_enh[key]["restored_diff"] = back.strip()
            mut_enh[key]["restored_ok"] = back == enhanced_baseline
        report["gate3_enhanced"] = mut_enh

        still_blind = [k for k, v in mut_enh.items() if not v["turned_red"]]
        report["still_blind_after_enhance"] = still_blind

        if not still_blind:
            h = write_enhanced_probe_to_aro(enhanced)
            report["promoted_enhanced_probe_sha256"] = h
            log(f"promoted enhanced probe sha={h}")
        else:
            log(f"STILL BLIND: {still_blind} — will not expand editable")

        g3_pass = all(v.get("turned_red") and v.get("restored_ok") for v in mut_enh.values())
        g1_pass = report["gate1_enhanced"].get("identical", False)
        report["gates"] = {
            "fingerprint": bool(g1_pass),
            "call_trace": bool(report.get("gate2", {}).get("gate2_pass")),
            "mutation": bool(g3_pass),
        }
        report["all_gates_pass"] = all(report["gates"].values())
        report["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        (OUT / "gates-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
        log(f"DONE all_gates_pass={report['all_gates_pass']} gates={report['gates']}")
        return 0 if report["all_gates_pass"] else 2

    except Exception as e:
        report["error"] = str(e)
        report["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (OUT / "gates-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        )
        log(f"FAIL: {e}")
        return 1
    finally:
        errs = cleanup(SOURCE_REPO, worktree, reservation)
        if errs:
            log(f"cleanup notes: {errs}")
        try:
            st = run(["git", "-C", str(SOURCE_REPO), "status", "-sb"]).stdout
            log(f"source status: {st.strip()}")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
