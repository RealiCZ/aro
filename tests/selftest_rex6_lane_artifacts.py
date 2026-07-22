"""Pre-spec contract for the aligned REX6 SSTORE/LOG probe pair.

Static by design: it freezes the auditable workload and output contracts before a
future target spec is allowed to consume the probes. Cargo execution is retained
as separate pre-spec evidence against the pinned target baseline.
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMED = ROOT / "probes/mega_evm_rex6_sstore_log.rs"
DIFF = ROOT / "probes/mega_evm_rex6_sstore_log_diff.rs"
DATA_DIR = ROOT / "docs/data/mega-evm-rex6-lanes-20260722/sstore-log"
VALIDATOR = DATA_DIR / "validate.py"
WORKLOAD_ID = "mega-evm-rex6-sstore-log"
WORKLOAD_VERSION = "3"
VARIANTS = (
    "zero_to_nonzero",
    "nonzero_to_nonzero",
    "reset_to_original",
    "sload",
    "sstore_sload",
    "log0_32",
    "log1_64",
    "log2_256",
)


def _source(path: Path) -> str:
    assert path.is_file(), f"planned probe missing: {path}"
    return path.read_text()


def _function(source: str, name: str) -> str:
    """Extract one Rust function by brace depth for exact pair alignment."""
    definitions = re.findall(rf"(?m)^fn {re.escape(name)}\(", source)
    assert len(definitions) == 1, (name, len(definitions))
    start = source.index(f"fn {name}(")
    brace = source.index("{", start)
    depth = 0
    for pos in range(brace, len(source)):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
            if depth == 0:
                return source[start:pos + 1]
    raise AssertionError(f"unterminated Rust function: {name}")


def _marked_setup(source: str) -> str:
    """Extract the complete setup immediately preceding the transact call."""
    begin = "    // COMMON_SETUP_BEGIN\n"
    end = "    // COMMON_SETUP_END\n"
    assert source.count(begin) == 1, source.count(begin)
    assert source.count(end) == 1, source.count(end)
    start = source.index(begin)
    finish = source.index(end, start) + len(end)
    return source[start:finish]


def _const(source: str, name: str) -> str:
    return next(line for line in source.splitlines() if line.startswith(f"const {name}:"))


def case_69():
    """T49: Lane 1 has one exact production tuple and canonical BENCH/DIFF contracts."""
    timed = _source(TIMED)
    diff = _source(DIFF)

    for label, src in (("timed", timed), ("differential", diff)):
        assert f'const WORKLOAD_ID: &str = "{WORKLOAD_ID}";' in src, label
        assert f'const WORKLOAD_VERSION: &str = "{WORKLOAD_VERSION}";' in src, label
        assert "MegaSpecId::REX6" in src, label
        assert "MegaEvm" in src and "MegaTransaction" in src, label
        assert ".transact(mega_tx)" in src, label
        assert "transact_raw" not in src, label
        assert "mega_tx.enveloped_tx = Some(Bytes::new())" in src, label
        for variant in VARIANTS:
            assert f'"{variant}"' in src, (label, variant)

        # Both sides must spell the same fixed production transaction tuple.
        for token in (
            "const CALLER:", "const CONTRACT:",
            "const GAS_LIMIT: u64 = 10_000_000_000;",
            "const STORAGE_ITERATIONS: usize = 100;",
            "const LOG_ITERATIONS: usize = 50;",
            ".caller(CALLER)", ".call(CONTRACT)", ".gas_limit(GAS_LIMIT)",
            ".value(U256::ZERO)", ".data(Bytes::new())", ".build_fill()",
            "NoOpInspector", "EmptyExternalEnv", "operator_fee_scalar = Some(U256::ZERO)",
            "operator_fee_constant = Some(U256::ZERO)",
        ):
            assert token in src, (label, token)

    # One logical workload uses byte-identical builders, ordered scenarios, and a
    # byte-identical complete setup block from fresh DB through transaction envelope.
    for name in ("push_number", "storage_scenario", "log_scenario", "scenarios"):
        assert _function(timed, name) == _function(diff, name), name
    assert _marked_setup(timed) == _marked_setup(diff)
    setup = _marked_setup(timed)
    assert setup + '    let result = evm.transact(mega_tx).expect("mega transact");' in timed
    assert setup + "    let returned = match evm.transact(mega_tx) {" in diff
    for required in (
        "CacheDB::<EmptyDB>::default()", "Bytecode::new_legacy", "code_hash",
        "db.insert_account_info(\n        CONTRACT", "code: Some(bytecode)",
        "db.insert_account_info(\n        CALLER", "balance:",
        "for &(slot, value) in &scenario.storage", "insert_account_storage",
        "MegaContext::new(db, SPEC)", "operator_fee_scalar = Some(U256::ZERO)",
        "operator_fee_constant = Some(U256::ZERO)",
        "MegaEvm::<_, NoOpInspector, EmptyExternalEnv>::new(context)",
        "TxEnvBuilder::new()", ".caller(CALLER)", ".call(CONTRACT)",
        ".gas_limit(GAS_LIMIT)", ".value(U256::ZERO)", ".data(Bytes::new())",
        ".build_fill()", "MegaTransaction::new(tx_env)",
        "mega_tx.enveloped_tx = Some(Bytes::new())",
    ):
        assert required in setup, required
    scenario_fn = _function(timed, "scenarios")
    positions = []
    for variant in VARIANTS:
        token = (f'storage_scenario("{variant}")' if not variant.startswith("log")
                 else f'log_scenario("{variant}"')
        positions.append(scenario_fn.index(token))
    assert positions == sorted(positions), positions

    storage_fn = _function(timed, "storage_scenario")
    sload_arm = storage_fn.split('"sload" => {', 1)[1].split("}", 1)[0]
    assert "storage.push" not in sload_arm, "#330 sload has no storage preseed"
    combined_arm = storage_fn.split('"sstore_sload" => {', 1)[1].split("}", 1)[0]
    assert "storage.push" not in combined_arm, "#330 sstore+sload has no preseed"
    for token in ("i + 1", "code.push(SSTORE)",
                  "code.extend_from_slice(&[SLOAD, POP])"):
        assert token in combined_arm, token
    for name in (
        "WORKLOAD_ID", "WORKLOAD_VERSION", "SPEC", "CALLER", "CONTRACT",
        "GAS_LIMIT", "STORAGE_ITERATIONS", "LOG_ITERATIONS",
    ):
        assert _const(timed, name) == _const(diff, name), name
    # Match #330 BytecodeBuilder::push_number(u64): every numeric immediate is
    # fixed-width PUSH8, and execution terminates by end-of-bytecode (no STOP).
    for src in (timed, diff):
        push_fn = _function(src, "push_number")
        assert "value: u64" in push_fn
        assert "code.push(PUSH8)" in push_fn
        assert "value.to_be_bytes()" in push_fn
        for forbidden in ("PUSH1", "PUSH2", "STOP"):
            assert forbidden not in src, forbidden

    # Strong wrapper boundary: timed has one scenario loop/execution inside
    # run_workload. Main cannot execute scenarios directly; its only executions
    # are complete workloads in spin, one warmup, and nested sample/scale wrappers.
    timed_workload = _function(timed, "run_workload")
    assert timed_workload.count("for scenario in scenarios {") == 1
    assert timed_workload.count("run_scenario(scenario)") == 1
    assert timed.count("for scenario in scenarios {") == 1
    timed_main = _function(timed, "main")
    assert "run_scenario(" not in timed_main
    assert timed_main.count("let scenarios = scenarios();") == 1
    assert timed_main.count("run_workload(&scenarios)") == 3
    assert "while Instant::now() < deadline {\n            acc = acc.wrapping_add(run_workload(&scenarios));" in timed_main
    assert "acc = acc.wrapping_add(run_workload(&scenarios));\n    let repetitions = scale;" in timed_main
    assert "for _ in 0..5 {" in timed_main
    assert "for _ in 0..repetitions {\n            acc = acc.wrapping_add(run_workload(&scenarios));" in timed_main
    assert 'println!("BENCH {}", line);' in timed_main

    # Differential has exactly one ordered scenario loop and one fold per
    # scenario, with no second workload or timed measurement machinery.
    diff_main = _function(diff, "main")
    assert diff_main.count("scenarios()") == 1
    assert diff_main.count("for scenario in scenarios() {") == 1
    assert diff_main.count("encode_outcome(&mut encoded, &scenario)") == 1
    assert diff.count("for scenario in scenarios() {") == 1
    assert "run_workload" not in diff
    assert "run_scenario" not in diff
    for forbidden in (
        "ARO_BENCH_SCALE", "Instant::now", "Duration::", "samples",
        "repetitions", "spin_secs",
    ):
        assert forbidden not in diff_main, forbidden
    assert 'println!("DIFF {digest:x}");' in diff_main

    # Deterministic differential: one cryptographic hash over explicit,
    # length-delimited byte encoding and deterministic ordering only.
    assert "keccak256" in next(line for line in diff.splitlines() if line.startswith("use alloy_primitives::"))
    assert "let digest = keccak256(encoded);" in diff_main
    assert "fnv" not in diff.lower()
    assert "fold_len" not in diff
    assert "fn push_len(encoded: &mut Vec<u8>, len: usize)" in diff
    assert "(len as u64).to_be_bytes()" in diff
    for forbidden in ("{:?}", "{:#?}", "HashMap", "format!(\"{:?}", ".values()"):
        assert forbidden not in diff, forbidden
    for required in (
        "fn encode_outcome", "to_be_bytes", "sort_unstable", "result.logs()",
        "present_value", "state_entries.sort_unstable_by", "slots.sort_unstable_by",
    ):
        assert required in diff, required

    # Scale zero is rejected before warmup, timing, or division and has a stable
    # error contract instead of producing NaN samples.
    zero_guard = 'assert!(scale > 0, "ARO_BENCH_SCALE must be greater than zero");'
    assert zero_guard in timed_main
    assert timed_main.index(zero_guard) < timed_main.index("run_workload(&scenarios)")
    assert timed_main.index(zero_guard) < timed_main.index("repetitions as f64")

    # Bucket IDs are intentionally absent: EmptyExternalEnv exposes no bucket IDs.
    assert "get_bucket_ids" not in timed
    assert "get_bucket_ids" not in diff
    assert "EmptyExternalEnv" in timed and "EmptyExternalEnv" in diff

    # Reproduction is checked in: real ARO injection APIs, detached baseline
    # worktree, unconditional cleanup, raw outputs, JSON, and verified hashes.
    validator = _source(VALIDATOR)
    for token in (
        "worktree\", \"add\", \"--detach", "finally:", "SpecTarget",
        "target.write_probe", "target.bench", "target.run_diff_probe",
        "target._cargo_run", "shutil.rmtree", "SHA256SUMS",
        "fcntl.flock", "LOCK_EX", ".aro-runs", "locks", "tempfile.mkdtemp",
        "os.replace", "cleanup_validation_run", "target._td_root",
        "worktree registration survived cleanup", "target directory survived cleanup",
        "LANE1_INJECT_FAILURE",
    ):
        assert token in validator, token
    assert "ignore_errors=True" not in validator
    assert "[0-9a-f]{64}" in validator
    assert 'scale=0' in validator
    manifest = _source(DATA_DIR / "SHA256SUMS")
    manifest_names = (
        "validate.py", "../../../../probes/mega_evm_rex6_sstore_log.rs",
        "../../../../probes/mega_evm_rex6_sstore_log_diff.rs", "bench.stdout",
        "diff-run1.stdout", "diff-run2.stdout", "validation.json",
    )
    for name in manifest_names:
        assert f"  {name}\n" in manifest, name
        expected = next(line.split()[0] for line in manifest.splitlines()
                        if line.endswith(f"  {name}"))
        actual = hashlib.sha256((DATA_DIR / name).resolve().read_bytes()).hexdigest()
        assert actual == expected, (name, actual, expected)
    validation = __import__("json").loads(_source(DATA_DIR / "validation.json"))
    assert validation["aro_head"]
    assert validation["baseline"]
    assert validation["probe_sha256"] == {
        "probes/mega_evm_rex6_sstore_log.rs": hashlib.sha256(TIMED.read_bytes()).hexdigest(),
        "probes/mega_evm_rex6_sstore_log_diff.rs": hashlib.sha256(DIFF.read_bytes()).hexdigest(),
    }
    print("case_69 OK: aligned REX6 SSTORE/LOG pre-spec probe contracts")
