#!/usr/bin/env python3
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("gate", HERE / "call_trace_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

SOURCES = {
    "crates/mega-evm/src/evm/host.rs",
    "crates/mega-evm/src/evm/instructions.rs",
}
WORKTREE = Path("/owned/worktree")


def fixture(body, events="Ir", summary="100"):
    return f"version: 1\npositions: line\nevents: {events}\n{body}summary: {summary}\n"


class ParserTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "raw"
            p.write_text(text)
            return gate.parse_callgrind(p, WORKTREE, SOURCES)

    def test_valid_compressed_ids_inline_files_and_multi_events_relative_positions(self):
        text = fixture(
            "fl=(1) /owned/worktree/crates/mega-evm/src/evm/host.rs\n"
            "fn=(1) host::sload\n10 4 1\n"
            "fi=(2) crates/mega-evm/src/evm/instructions.rs\n"
            "fn=(2) instructions::sstore\n+2 6 2\n"
            "fe=(1)\nfn=(1)\n* 3 4\n"
            "cfi=(2)\ncfn=(2)\ncalls=2 +5\n+7 8 9\n",
            events="Ir Dr", summary="21 16",
        )
        got = self.parse(text)
        self.assertEqual(got["summary_ir"], 21)
        self.assertEqual(got["reached_files"], sorted(SOURCES))
        called = [r for r in got["evidence"] if r["called_ir"]]
        self.assertEqual(called[0]["called_ir"], 8)
        self.assertEqual(called[0]["call_count"], 2)

    def test_anchored_normalization_and_baseline_membership(self):
        self.assertEqual(gate.normalize_target_source("crates/mega-evm/src/evm/host.rs", WORKTREE, SOURCES), "crates/mega-evm/src/evm/host.rs")
        self.assertEqual(gate.normalize_target_source("/owned/worktree/crates/mega-evm/src/evm/host.rs", WORKTREE, SOURCES), "crates/mega-evm/src/evm/host.rs")
        for bad in ("/tmp/prefix/crates/mega-evm/src/evm/host.rs", "x/crates/mega-evm/src/evm/host.rs", "crates/mega-evm/src/evm/missing.rs", "/owned/worktree/../worktree/crates/mega-evm/src/evm/host.rs"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                gate.normalize_target_source(bad, WORKTREE, SOURCES)

    def test_malformed_undefined_and_dangling_records_fail_closed(self):
        bad_bodies = [
            "fl=(9)\nfn=x\n1 1\n",                       # undefined file id
            "fl=crates/mega-evm/src/evm/host.rs\nfn=(x\n1 1\n", # malformed ID
            "fl=crates/mega-evm/src/evm/host.rs\nfn=x\ncfn=(9)\ncalls=1 2\n2 1\n", # undefined fn id
            "fl=crates/mega-evm/src/evm/host.rs\nfn=x\ncfl=crates/mega-evm/src/evm/host.rs\ncfn=y\ncalls=nope 2\n2 1\n",
            "fl=crates/mega-evm/src/evm/host.rs\nfn=x\n1 nope\n",
            "fl=crates/mega-evm/src/evm/host.rs\nfn=x\ncfl=crates/mega-evm/src/evm/host.rs\ncfn=y\ncalls=1 2\n", # dangling call
            "fl=crates/mega-evm/src/evm/host.rs\nfn=x\ncalls=1 2\n2 1\n", # missing callee
        ]
        for body in bad_bodies:
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                self.parse(fixture(body))

    def test_event_schema_and_summary_are_strict(self):
        with self.assertRaises(RuntimeError): self.parse(fixture("fl=crates/mega-evm/src/evm/host.rs\nfn=x\n1 2\n", events="Dr", summary="2"))
        with self.assertRaises(RuntimeError): self.parse(fixture("fl=crates/mega-evm/src/evm/host.rs\nfn=x\n1 2 3\n", events="Ir Dr", summary="2"))


class OwnershipAndProcessTests(unittest.TestCase):
    def test_collision_and_dangling_symlink_fail_closed_and_survive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            collision = root / "collision"
            collision.mkdir(); (collision / "unrelated").write_text("keep")
            with self.assertRaises(RuntimeError): gate.claim_absent_path(collision, "secret")
            self.assertEqual((collision / "unrelated").read_text(), "keep")
            dangling = root / "dangling"; dangling.symlink_to(root / "missing")
            with self.assertRaises(RuntimeError): gate.claim_absent_path(dangling, "secret")
            self.assertTrue(os.path.lexists(dangling))

    def test_cleanup_requires_matching_marker(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "owned"; p.mkdir(); (p / gate.OWNERSHIP_MARKER).write_text("other")
            (p / "keep").write_text("yes")
            with self.assertRaises(RuntimeError): gate.remove_owned(p, "mine")
            self.assertEqual((p / "keep").read_text(), "yes")

    def test_timeout_kills_and_reaps_process_group(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            gate.run_process([sys.executable, "-c", "import subprocess,time,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); time.sleep(30)"], timeout=0.2)
        time.sleep(0.1)
        self.assertEqual(gate.live_group_members(gate.LAST_PROCESS_GROUP), [])


class SelectionTests(unittest.TestCase):
    def test_every_editable_requires_per_file_operation_evidence_in_both_traces(self):
        rows=[]
        for path, regexes in gate.EDITABLE_FUNCTION_POLICY.items():
            fn = gate.POLICY_TEST_FUNCTIONS[path]
            rows.append({"file":path,"function":fn,"self_ir":1,"called_ir":0,"call_count":0,"locations":["1"]})
        parsed={"raw_file":"x","summary_ir":100,"reached_files":sorted(gate.EDITABLE_FUNCTION_POLICY),"evidence":rows}
        _, proposed=gate.derive_selection(parsed, json.loads(json.dumps(parsed)))
        self.assertEqual(proposed["proposed_editable"], sorted(gate.EDITABLE_FUNCTION_POLICY))
        self.assertEqual(set(proposed["per_file_operation_evidence"]), set(gate.EDITABLE_FUNCTION_POLICY))
        broken=json.loads(json.dumps(parsed)); broken["evidence"][0]["function"]="generic::new"
        with self.assertRaises(RuntimeError): gate.derive_selection(parsed, broken)


if __name__ == "__main__":
    unittest.main(verbosity=2)
