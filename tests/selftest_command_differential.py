"""External-command differential: `correctness_oracle.differential.command`.

A differential that is a whole program run in the worktree (e.g. a conformance runner over a
fixture corpus) rather than a cargo example of the target. Hermetic: the "command" is a tiny
Python script in a temp dir; no cargo, no git, no network.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# The fake oracle: prints the worktree's behaviour.txt as its fingerprint, proves it ran in the
# worktree with the worktree's own CARGO_TARGET_DIR, and refuses (exit 3) when the worktree
# holds a `refuse` marker.
_PROBE = r'''
import os, sys
from pathlib import Path
work = Path(sys.argv[1])
assert Path.cwd().resolve() == work.resolve(), (Path.cwd(), work)
assert os.environ.get("CARGO_TARGET_DIR"), "no per-worktree CARGO_TARGET_DIR"
if (work / "refuse").exists():
    print("RUN equivalence-Osaka: 0 lines", flush=True)
    sys.stderr.write("refusing: a run printed no lines\n")
    sys.exit(3)
print("RUN info line, not the fingerprint")
print("DIFF " + (work / "behaviour.txt").read_text().strip())
'''


def _spec_dict(probe: str, repo: str, **diff):
    d = {"probe": probe, "prefix": "DIFF",
         "command": [sys.executable, "-I", "{probe}", "{work}"]}
    d.update(diff)
    return {
        "name": "cmd-diff-selftest",
        "target_repo": {"path": repo, "baseline_ref": "HEAD"},
        "metric": "ns_per_call",
        "benchmark_probe": {"pkg": "p", "example": "e",
                            "probe": "fixtures/mini-target/probes/mini_target.rs"},
        "correctness_oracle": {"build": ["true"], "test": ["true"], "differential": d},
        "constraints": {"editable": ["src"]},
        "run": {"timeout": 60},
    }


def case_70():
    """Command differential: spec validation of both differential shapes."""
    from aro import spec as specmod
    print("=== case 70: command differential — spec shapes ===")
    with tempfile.TemporaryDirectory() as tmp:
        probe = str(Path(tmp) / "oracle.py")
        Path(probe).write_text(_PROBE)
        sp = specmod.from_dict(_spec_dict(probe, tmp))
        assert sp.differential["command"][2] == "{probe}"
        specmod.validate_artifacts(sp)   # the probe file exists

        # command present: pkg/example not required; probe + prefix still are.
        for missing in ("probe", "prefix"):
            d = _spec_dict(probe, tmp)
            d["correctness_oracle"]["differential"].pop(missing)
            try:
                specmod.from_dict(d)
            except specmod.SpecError as e:
                assert missing in str(e), e
            else:
                raise AssertionError(f"command differential without {missing} loaded")

        # command must be a non-empty list of non-empty strings.
        for bad in ([], "python3 oracle.py", ["python3", 3], ["python3", ""]):
            try:
                specmod.from_dict(_spec_dict(probe, tmp, command=bad))
            except specmod.SpecError as e:
                assert "command" in str(e), e
            else:
                raise AssertionError(f"bad command {bad!r} loaded")

        # The cargo-example shape is unchanged: without command, pkg + example are required.
        d = _spec_dict(probe, tmp)
        d["correctness_oracle"]["differential"] = {"probe": probe, "prefix": "DIFF"}
        try:
            specmod.from_dict(d)
        except specmod.SpecError as e:
            assert "pkg" in str(e) and "example" in str(e), e
        else:
            raise AssertionError("example differential without pkg/example loaded")

        # A missing probe file fails at load, as for an example probe.
        sp_missing = specmod.from_dict(_spec_dict(str(Path(tmp) / "nope.py"), tmp))
        try:
            specmod.validate_artifacts(sp_missing)
        except specmod.SpecError as e:
            assert "differential.probe" in str(e), e
        else:
            raise AssertionError("missing differential probe file passed validation")
    print("case_70 OK: command differential spec shapes")


def case_71():
    """Command differential: SpecTarget runs it per worktree and compares the DIFF lines."""
    from aro import spec as specmod
    from aro.target import SpecTarget
    print("=== case 71: command differential — SpecTarget ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        probe = tmp / "oracle.py"
        probe.write_text(_PROBE)
        repo = tmp / "repo"
        repo.mkdir()
        t = SpecTarget(specmod.from_dict(_spec_dict(str(probe), str(repo))))
        base, cand = tmp / "wt-base", tmp / "wt-cand"
        for w in (base, cand):
            w.mkdir()
            (w / "behaviour.txt").write_text("v1 equivalence-Osaka=aa satin-Osaka=bb\n")

        # Same behaviour → equal; the DIFF line (not the RUN line) is the fingerprint.
        assert t.differential(cand, base) is True
        assert t.last_differential == {
            "baseline": "DIFF v1 equivalence-Osaka=aa satin-Osaka=bb",
            "candidate": "DIFF v1 equivalence-Osaka=aa satin-Osaka=bb"}, t.last_differential
        # Each side ran in its own worktree with its own target dir.
        assert t.td_for(base) != t.td_for(cand)

        # Different behaviour → not equal; both lines kept for the detail.
        (cand / "behaviour.txt").write_text("v1 equivalence-Osaka=aa satin-Osaka=cc\n")
        assert t.differential(cand, base) is False
        assert t.last_differential["candidate"].endswith("satin-Osaka=cc")

        # A refusing side (non-zero exit) is an error naming the exit, never a match.
        (cand / "refuse").write_text("")
        try:
            t.differential(cand, base)
        except RuntimeError as e:
            assert "exited 3" in str(e) and "printed no lines" in str(e), e
        else:
            raise AssertionError("a refusing differential command compared equal")
        assert t.last_differential is None, "a failed comparison must not leave evidence"

        # A command that exits 0 without the prefix line is an error too.
        (cand / "refuse").unlink()
        quiet = tmp / "quiet.py"
        quiet.write_text("print('no fingerprint here')\n")
        t2 = SpecTarget(specmod.from_dict(_spec_dict(str(quiet), str(repo))))
        try:
            t2.differential(cand, base)
        except RuntimeError as e:
            assert "no output" in str(e), e
        else:
            raise AssertionError("a command without a DIFF line compared equal")
    print("case_71 OK: command differential runs per worktree")


def case_72():
    """Command differential: the gate's mismatch detail carries both fingerprints."""
    from aro.eval import run_correctness_gates
    print("=== case 72: differential evidence in the gate detail ===")

    class _Target:
        differential_required = True
        has_differential = True

        def __init__(self, base_fp, cand_fp):
            self._fps = (base_fp, cand_fp)
            self.last_differential = None

        def build(self, work):
            pass

        def test(self, work):
            return 7

        def differential(self, work, baseline):
            self.last_differential = {"baseline": self._fps[0], "candidate": self._fps[1]}
            return self._fps[0] == self._fps[1]

    g = run_correctness_gates(_Target("DIFF a=1 b=2", "DIFF a=1 b=3"), "/c", "/b",
                              skip_build=True)
    assert not g["ok"] and g["failing_gate"] == "differential", g
    assert g["detail"].startswith("differential check failed: behavior differs from baseline")
    assert "baseline:  DIFF a=1 b=2" in g["detail"] and "candidate: DIFF a=1 b=3" in g["detail"]

    g = run_correctness_gates(_Target("DIFF a=1", "DIFF a=1"), "/c", "/b", skip_build=True)
    assert g["ok"] and g["gates"]["differential"] == "ok", g

    # A target without last_differential (every older target) keeps the historical string.
    class _Legacy(_Target):
        def differential(self, work, baseline):
            return False
    g = run_correctness_gates(_Legacy("x", "y"), "/c", "/b", skip_build=True)
    assert g["detail"] == "differential check failed: behavior differs from baseline", g
    print("case_72 OK: differential evidence in the gate detail")


def case_73():
    """Command differential: an accepted gate records the fingerprint both sides matched."""
    from aro import eval as evalmod
    from aro.types import Candidate, Edit, NoiseFloors, Objective, Patch
    from tests.common import MockTarget
    print("=== case 73: differential fingerprint on the gate event ===")

    class _Events:
        def __init__(self):
            self.rows = []

        def emit(self, event, **f):
            self.rows.append({"event": event, **f})

    class _Fingerprinted(MockTarget):
        has_differential = True
        differential_required = True

        def differential(self, work, baseline):
            fp = "DIFF 0123abcd equivalence-Osaka=19465:0:5d33c0d2c570"
            self.last_differential = {"baseline": fp, "candidate": fp}
            return True

    def _no_bench(*a, **k):
        raise evalmod._BenchError("stop after Gate 1")

    orig = evalmod._significance
    evalmod._significance = _no_bench
    try:
        events = _Events()
        cand = Candidate(id="c1", hypothesis="h", patch=Patch([Edit("src/opt.rs", "a", "b")]))
        evalmod.evaluate(_Fingerprinted(), "base", Patch([]), cand, 2, NoiseFloors(),
                         [Objective("metric/x")], events=events)
    finally:
        evalmod._significance = orig
    gate = [r for r in events.rows if r["event"] == "gate" and r.get("gate") == "differential"]
    assert len(gate) == 1 and gate[0]["status"] == "ok", events.rows
    assert gate[0]["fingerprint"].startswith("DIFF 0123abcd equivalence-Osaka="), gate[0]

    # A target without the evidence attribute emits the historical event, no fingerprint key.
    class _Plain(MockTarget):
        has_differential = True
        differential_required = True

    evalmod._significance = _no_bench
    try:
        events = _Events()
        evalmod.evaluate(_Plain(), "base", Patch([]), cand, 2, NoiseFloors(),
                         [Objective("metric/x")], events=events)
    finally:
        evalmod._significance = orig
    gate = [r for r in events.rows if r["event"] == "gate" and r.get("gate") == "differential"]
    assert gate and "fingerprint" not in gate[0], gate
    print("case_73 OK: differential fingerprint on the gate event")
