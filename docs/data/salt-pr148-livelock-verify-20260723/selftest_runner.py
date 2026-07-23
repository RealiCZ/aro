#!/usr/bin/env python3
"""Fast selftests for salt_pr148_runner.py. Never invokes Cargo or a campaign."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
RUNNER_PATH = HERE / "salt_pr148_runner.py"
SPEC = importlib.util.spec_from_file_location("salt_pr148_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class RunnerSelfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="salt-runner-selftest-")
        self.tmp = Path(self.tmp_obj.name)
        self.evidence = self.tmp / "evidence"
        self.scan = self.tmp / "scan"
        self.scan.mkdir()
        self.store = runner.EvidenceStore(self.evidence)
        self.cmd = runner.CommandRunner(self.store, [self.scan], runtime_timeout=1.0, term_grace=0.15)

    def tearDown(self) -> None:
        self.tmp_obj.cleanup()

    def key(self, label: str) -> str:
        return f"selftest/{label}"

    def run_py(self, label: str, code: str, timeout: float = 1.0, env: dict[str, str] | None = None):
        return self.cmd.run(
            trial_key=self.key(label), phase="selftest", cohort=label,
            argv=[sys.executable, "-c", code], cwd=self.scan,
            env_delta=env, timeout=timeout,
        )

    def wait_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
            time.sleep(0.02)
        self.assertFalse(Path(f"/proc/{pid}").exists(), f"process remains: {pid}")

    def test_env_contamination_is_stripped_then_explicit_env_applied(self) -> None:
        poison = {
            "RUST_TEST_THREADS": "99", "RUSTUP_TOOLCHAIN": "evil", "RUSTFLAGS": "-Zevil",
            "CARGO_ENCODED_RUSTFLAGS": "evil", "CARGO_HOME": "/evil", "CARGO_BUILD_JOBS": "99",
            "NUM_DATA_BUCKETS": "999", "BUCKET_RESIZE_LOAD_FACTOR_PCT": "999", "RANDOM_OPS": "999",
            "RAYON_NUM_THREADS": "99", "TOKIO_WORKER_THREADS": "99", "MAKEFLAGS": "-j99",
            "GH_TOKEN": "top-secret", "AWS_SECRET_ACCESS_KEY": "top-secret-2",
        }
        script = "import json,os; print(json.dumps({k:os.environ.get(k) for k in " + repr(list(poison)) + "+['SAFE','NUM_DATA_BUCKETS']}))"
        with mock.patch.dict(os.environ, poison, clear=False):
            record = self.run_py("env", script, env={"SAFE": "yes", "NUM_DATA_BUCKETS": "2"})
        payload = json.loads((self.evidence / record.log_path).read_text().splitlines()[-1])
        self.assertEqual(payload["SAFE"], "yes")
        self.assertEqual(payload["NUM_DATA_BUCKETS"], "2")
        for key in poison:
            if key != "NUM_DATA_BUCKETS":
                self.assertIsNone(payload[key], key)
        self.assertTrue(set(poison).issubset(record.stripped_env_names))
        serialized = json.dumps(record.to_dict())
        self.assertNotIn("top-secret", serialized)
        self.assertRegex(record.safe_env_fingerprint, r"^[0-9a-f]{64}$")

    def test_timeout_kills_entire_process_group(self) -> None:
        pidfile = self.tmp / "child.pid"
        code = (
            "import pathlib,signal,subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']);"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
        )
        record = self.run_py("timeout", code, timeout=0.2)
        self.assertEqual(record.status, "timeout")
        self.assertGreaterEqual(record.cleanup_elapsed_seconds, 0)
        self.assertEqual(record.residual_pids, [])
        self.wait_gone(int(pidfile.read_text()))

    def test_normal_leader_exit_kills_orphan_descendant(self) -> None:
        pidfile = self.tmp / "normal-child.pid"
        code = (
            "import pathlib,subprocess,sys;"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))"
        )
        record = self.run_py("normal-orphan", code)
        self.assertEqual(record.status, "pass")
        self.assertTrue(record.cleanup_actions)
        self.wait_gone(int(pidfile.read_text()))

    def test_process_elapsed_excludes_slow_cleanup(self) -> None:
        pidfile = self.tmp / "slow-child.pid"
        code = (
            "import pathlib,signal,subprocess,sys;"
            "p=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)']);"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))"
        )
        record = self.run_py("timing", code)
        self.assertLess(record.process_elapsed_seconds, 0.5)
        self.assertGreater(record.cleanup_elapsed_seconds, 0.01)
        self.assertEqual(record.elapsed_seconds, record.process_elapsed_seconds)
        self.wait_gone(int(pidfile.read_text()))

    def test_popen_failure_always_persists_attempt_record(self) -> None:
        record = self.cmd.run(
            trial_key=self.key("popen"), phase="selftest", cohort="popen",
            argv=[str(self.tmp / "does-not-exist")], cwd=self.scan,
        )
        self.assertEqual(record.status, "launch-error")
        self.assertIn("FileNotFoundError", record.error)
        self.assertEqual(self.store.by_trial_key()[self.key("popen")]["status"], "launch-error")

    def test_interrupted_wait_persists_attempt_and_cleans_group(self) -> None:
        real_wait = subprocess.Popen.wait
        calls = 0

        def interrupt_once(proc, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt
            return real_wait(proc, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, "wait", interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                self.run_py("interrupt", "import time; time.sleep(60)")
        row = self.store.by_trial_key()[self.key("interrupt")]
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["residual_pids"], [])

    def test_immutable_store_rejects_duplicate_trial_key(self) -> None:
        first = self.run_py("duplicate", "pass")
        self.assertEqual(first.status, "pass")
        with self.assertRaises(runner.DuplicateTrialKey):
            self.run_py("duplicate", "pass")

    def test_partial_resume_skips_completed_control_and_only_passing_pr(self) -> None:
        existing = {
            "campaign/control/control/ordinary/01": {"status": "timeout", "timeout": True, "residual_pids": []},
            "campaign/experiment/pr/ordinary/01": {"status": "pass", "timeout": False, "residual_pids": []},
        }
        self.assertTrue(runner.resume_completed(existing["campaign/control/control/ordinary/01"], fail_closed=False))
        self.assertTrue(runner.resume_completed(existing["campaign/experiment/pr/ordinary/01"], fail_closed=True))
        self.assertFalse(runner.resume_completed({"status": "fail", "residual_pids": []}, fail_closed=True))

    def test_prior_fail_in_pr_gate_aborts_resume(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "prior fail-closed"):
            runner.resume_completed({"status": "fail", "residual_pids": []}, fail_closed=True, abort_prior_failure=True)

    def test_exact_gates_require_exact_keys_and_identity_hashes(self) -> None:
        plan = runner.campaign_plan()
        expected = runner.expected_gate_identities(plan)
        records = []
        for gate in ("experiment_all_passed", "regressions_all_passed", "conformance_all_passed"):
            records.extend({"trial_key": k, "identity_sha256": h, "status": "pass", "residual_pids": []} for k, h in expected[gate].items())
        gates = runner.compute_gates(records, plan)
        self.assertTrue(gates["experiment_all_passed"])
        self.assertTrue(gates["regressions_all_passed"])
        self.assertTrue(gates["conformance_all_passed"])
        self.assertFalse(runner.compute_gates(records[:-1], plan)["conformance_all_passed"])
        duplicate = records + [dict(records[0], run_id="duplicate")]
        self.assertFalse(runner.compute_gates(duplicate, plan)["experiment_all_passed"])
        bad = [dict(r) for r in records]
        bad[0]["identity_sha256"] = "0" * 64
        self.assertFalse(runner.compute_gates(bad, plan)["experiment_all_passed"])

    def test_zero_run_residual_gate_is_null(self) -> None:
        gates = runner.compute_gates([], runner.campaign_plan())
        self.assertIsNone(gates["no_process_residuals"])
        self.assertFalse(gates["pr148_validation_passed"])

    def test_conformance_exact_historical_order_and_dated_nightly(self) -> None:
        entries = runner.campaign_plan()["conformance"]["entries"]
        self.assertEqual([e["name"] for e in entries], [
            "check", "cargo-sort", "test", "test-bucket-resize", "random-stress",
            "no-std-check", "no-default-features-test", "no-default-features-resize-test", "fmt", "clippy",
        ])
        for index in (5, 6, 7):
            self.assertEqual(entries[index]["argv"][1], "+nightly-2026-03-20")
        self.assertFalse(any("--test-threads" in x for e in entries for x in e["argv"]))
        self.assertNotIn("--locked", entries[1]["argv"])
        self.assertNotIn("--locked", entries[8]["argv"])

    def test_plan_has_exact_shas_hashes_argv_env_and_binary_names(self) -> None:
        plan = runner.campaign_plan()
        self.assertEqual(plan["checkouts"]["control"]["head"], runner.CONTROL_SHA)
        self.assertEqual(plan["checkouts"]["pr"]["head"], runner.PR_SHA)
        self.assertEqual(set(plan["regressions"]), {"shared_committer_init", "shared_committer_init_os_winner"})
        for section in ("control", "experiment", "regressions"):
            for entry in plan[section].values():
                self.assertIn("argv", entry)
                self.assertIn("env", entry)
                self.assertIn("count", entry)
                self.assertRegex(entry["identity_sha256"], r"^[0-9a-f]{64}$")

    def test_checkout_invariant_fails_wrong_sha_dirty_or_source_hash_change(self) -> None:
        expected = {"resolved_path": "/exact", "head": "a", "detached": True, "origin": "https://github.com/megaeth-labs/salt.git", "status": "", "tree": "t", "cargo_lock_sha256": "l"}
        runner.validate_checkout_snapshot("control", expected, dict(expected))
        for field, value in (("head", "b"), ("status", "?? bad"), ("tree", "changed"), ("cargo_lock_sha256", "changed"), ("resolved_path", "/wrong")):
            actual = dict(expected)
            actual[field] = value
            with self.assertRaisesRegex(RuntimeError, "checkout invariant"):
                runner.validate_checkout_snapshot("control", expected, actual)

    def test_refresh_manifest_preserves_state_summary_and_report(self) -> None:
        self.evidence.mkdir(exist_ok=True)
        state = self.evidence / "state.json"
        summary = self.evidence / "summary.json"
        report = self.evidence / "REPORT.md"
        state.write_text('{"campaign_id":"stable"}\n')
        summary.write_text('{"state":"keep"}\n')
        report.write_text("keep report\n")
        before = {p.name: p.read_bytes() for p in (state, summary, report)}
        runner.refresh_manifest(self.evidence)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (state, summary, report)})

    def test_init_cost_stats_are_paired_descriptive_and_predeclared(self) -> None:
        samples = []
        for pair in range(1, 21):
            order = ("control", "pr") if pair % 2 else ("pr", "control")
            for position, cohort in enumerate(order, 1):
                samples.append({"pair": pair, "position": position, "cohort": cohort, "seconds": 1.0 if cohort == "control" else 1.04})
        result = runner.init_cost_statistics(samples)
        self.assertEqual(result["pair_count"], 20)
        self.assertEqual(result["practical_threshold_relative"], 0.05)
        self.assertIn("paired_relative_deltas", result)
        self.assertIn("order_strata", result)
        self.assertNotIn("performance_claim_allowed", result)
        self.assertEqual(result["practical_interpretation"], "below-predeclared-5-percent-threshold")

    def test_capture_uses_command_runner_and_metadata_contains_complete_evidence(self) -> None:
        result = self.cmd.capture(trial_key=self.key("capture"), phase="metadata", cohort="tool", argv=[sys.executable, "-c", "print('hello')"], cwd=self.scan)
        self.assertEqual(result["stdout"], "hello\n")
        self.assertEqual(result["record"]["status"], "pass")
        self.assertIn(self.key("capture"), self.store.by_trial_key())

    def test_shell_strings_and_hostile_argv_are_safe(self) -> None:
        with self.assertRaises(TypeError):
            self.cmd.run(trial_key=self.key("bad"), phase="selftest", cohort="bad", argv="sleep 1", cwd=self.scan)
        marker = self.tmp / "injected"
        hostile = f";touch {marker}"
        record = self.cmd.run(trial_key=self.key("hostile"), phase="selftest", cohort="hostile", argv=[sys.executable, "-c", "import sys; print(sys.argv[1])", hostile], cwd=self.scan)
        self.assertEqual(record.status, "pass")
        self.assertFalse(marker.exists())

    def test_cli_has_no_no_resume_mode(self) -> None:
        source = RUNNER_PATH.read_text()
        self.assertNotIn("--no-resume", source)
        self.assertNotIn("shell=True", source)


class StateTests(unittest.TestCase):
    def test_campaign_id_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            first = runner.load_or_create_state(path, runner.campaign_plan())
            runner.atomic_write_json(path, first)
            second = runner.load_or_create_state(path, runner.campaign_plan())
            self.assertEqual(first["campaign_id"], second["campaign_id"])
            self.assertEqual(first["plan_sha256"], second["plan_sha256"])


def main() -> int:
    started = runner.utc_now()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    payload = {
        "started_utc": started, "ended_utc": runner.utc_now(), "tests_run": result.testsRun,
        "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
        "successful": result.wasSuccessful(), "campaign_started": False,
        "command": [sys.executable, str(Path(__file__).resolve())],
    }
    runner.atomic_write_json(HERE / "salt148-selftest-results.json", payload)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
