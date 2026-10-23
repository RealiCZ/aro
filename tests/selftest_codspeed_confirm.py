"""CodSpeed confirm layer (aro/confirm.py, spec `codspeed_confirm`).

Between the inner loop and the terminal gate: an accepted candidate's per-row probe Ir must
agree with the target's own CodSpeed rows, or the candidate is frozen for review
(`codspeed-unconfirmed`). Hermetic: fake measure documents, a fake valgrind, mock targets.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def _spec_dict(**extra):
    d = {
        "name": "confirm-selftest",
        "target_repo": {"path": "/tmp/does-not-need-to-exist-confirm", "baseline_ref": "HEAD"},
        "metric": "ns_per_call",
        "benchmark_probe": {"pkg": "p", "example": "e",
                            "probe": "fixtures/mini-target/probes/mini_target.rs"},
        "correctness_oracle": {"build": ["true"], "test": ["true"]},
        "constraints": {"editable": ["src"]},
        "terminal_bench_targets": ["transact", "block"],
        "measure_bin": "/bin/false",
        "control_lanes": ["op_revm"],
        "icount_epsilon_pct": 0.1,
        "terminal_default_floor_pct": 0.5,
        "codspeed_confirm": {"enabled": True},
    }
    d.update(extra)
    return d


def _expect_exit(fn, needle):
    try:
        fn()
    except SystemExit as e:
        assert needle in str(e), (needle, str(e))
    else:
        raise AssertionError(f"expected SystemExit mentioning {needle!r}")


def case_77():
    """CodSpeed confirm: the spec field — off by default, bench lane required, strict keys."""
    from aro import spec as specmod
    print("=== case 77: codspeed_confirm spec field ===")
    sp = specmod.from_dict(_spec_dict())
    assert sp.codspeed_confirm == {"enabled": True, "rounds": 1, "probe_iters": 8,
                                   "timeout_secs": None}, sp.codspeed_confirm
    d = _spec_dict()
    d.pop("codspeed_confirm")
    assert specmod.from_dict(d).codspeed_confirm == {}
    assert specmod.from_dict(_spec_dict(codspeed_confirm={"enabled": False})).codspeed_confirm == {}
    sp = specmod.from_dict(_spec_dict(codspeed_confirm={"enabled": True, "rounds": 3,
                                                        "probe_iters": 4, "timeout_secs": 900}))
    assert sp.codspeed_confirm["rounds"] == 3 and sp.codspeed_confirm["timeout_secs"] == 900.0

    _expect_exit(lambda: specmod.from_dict(_spec_dict(codspeed_confirm=True)), "object")
    _expect_exit(lambda: specmod.from_dict(_spec_dict(codspeed_confirm={"enabled": "yes"})),
                 "enabled")
    _expect_exit(lambda: specmod.from_dict(_spec_dict(codspeed_confirm={"enabled": True,
                                                                        "rows": 1})), "unknown")
    _expect_exit(lambda: specmod.from_dict(_spec_dict(codspeed_confirm={"enabled": True,
                                                                        "rounds": 0})), "rounds")
    # The layer measures the bench lane's targets: probe lane or no targets fails at load.
    _expect_exit(lambda: specmod.from_dict(_spec_dict(terminal_lane="probe")), "bench terminal lane")
    _expect_exit(lambda: specmod.from_dict(_spec_dict(terminal_bench_targets=[])),
                 "bench terminal lane")
    print("case_77 OK: codspeed_confirm spec field")


def case_78():
    """CodSpeed confirm: judge() — claimed rows must improve, no row may pass its floor."""
    from aro import confirm as cf
    print("=== case 78: confirm judge matrix ===")
    floor = lambda row: 0.5  # noqa: E731
    pb = {"transact/a/satin": 1000, "transact/b/satin": 1000, "block/t": 5000}
    pc = {"transact/a/satin": 980, "transact/b/satin": 1000, "block/t": 5000}   # a claims -2%
    cb = {"transact/a/satin": 2000, "transact/a/op_revm": 1500, "transact/b/satin": 3000}
    good = {"transact/a/satin": 1970, "transact/a/op_revm": 1600, "transact/b/satin": 3001}

    r = cf.judge(pb, pc, cb, good, epsilon_pct=0.1, floor_of=floor, control_lanes=["op_revm"],
                 targets=["transact"])
    assert r.status == cf.CONFIRMED, r.reasons
    assert r.claimed == ["transact/a/satin"] and r.affected == ["transact/a/satin"]
    ev = r.rows["transact/a/satin"]
    assert ev["probe_base"] == 1000 and ev["probe_cand"] == 980 and ev["claimed"]
    assert ev["codspeed_base"] == 2000 and ev["codspeed_cand"] == 1970 and ev["floor_pct"] == 0.5
    assert "transact/a/op_revm" not in r.rows, "control rows are not judged"
    assert r.to_event()["rows"]["transact/b/satin"]["codspeed_delta_pct"] > 0  # +0.03%: in floor

    # The claimed row does not improve in CodSpeed.
    r = cf.judge(pb, pc, cb, {**good, "transact/a/satin": 2001}, epsilon_pct=0.1,
                 floor_of=floor, control_lanes=["op_revm"])
    assert r.status == cf.DISAGREED and "did not improve" in r.reasons[0], r.reasons
    # Another row regresses past its floor.
    r = cf.judge(pb, pc, cb, {**good, "transact/b/satin": 3020}, epsilon_pct=0.1,
                 floor_of=floor, control_lanes=["op_revm"])
    assert r.status == cf.DISAGREED and any("regresses" in x for x in r.reasons), r.reasons
    # A control row may move freely.
    r = cf.judge(pb, pc, cb, {**good, "transact/a/op_revm": 9999}, epsilon_pct=0.1,
                 floor_of=floor, control_lanes=["op_revm"])
    assert r.status == cf.CONFIRMED, r.reasons
    # No claimed row: nothing CodSpeed could confirm.
    r = cf.judge(pb, pb, cb, cb, epsilon_pct=0.1, floor_of=floor)
    assert r.status == cf.DISAGREED and "no probe row improves" in r.reasons[0]
    # A claim below ε is not a claim.
    r = cf.judge(pb, {**pb, "transact/a/satin": 999.5}, cb, cb, epsilon_pct=0.1, floor_of=floor)
    assert r.claimed == [] and r.status == cf.DISAGREED
    # The claimed row is missing from CodSpeed: the mapping broke.
    r = cf.judge(pb, pc, {"transact/b/satin": 3000}, {"transact/b/satin": 3000},
                 epsilon_pct=0.1, floor_of=floor)
    assert any("not in the CodSpeed output" in x for x in r.reasons), r.reasons
    # Row sets that differ between sides are a disagreement.
    r = cf.judge(pb, {**pc, "extra/row": 1}, cb, good, epsilon_pct=0.1, floor_of=floor,
                 control_lanes=["op_revm"])
    assert any("probe row sets differ" in x for x in r.reasons), r.reasons
    r = cf.judge(pb, pc, cb, {k: v for k, v in good.items() if k != "transact/b/satin"},
                 epsilon_pct=0.1, floor_of=floor, control_lanes=["op_revm"])
    assert any("CodSpeed row sets differ" in x for x in r.reasons), r.reasons
    assert cf.target_of("block/transfers", ["transact", "block"]) == "block"
    assert cf.target_of("corpus/x", ["transact", "block"]) is None
    print("case_78 OK: confirm judge matrix")


class _FakeTarget:
    """Probe rows and per-row Ir from tables; worktrees are names."""

    def __init__(self, spec, probe, fail_rows=False):
        self.spec = spec
        self._probe = probe            # {work: {row: ir}}
        self._fail = fail_rows
        self.icount_calls = []

    def td_for(self, work):
        return Path(tempfile.gettempdir()) / "aro-confirm-selftest" / str(work)

    def probe_rows(self, work):
        if self._fail:
            raise RuntimeError("probe has no row mode")
        return sorted(self._probe[work])

    def icount_rows(self, work, rows, iters=8):
        self.icount_calls.append((work, tuple(rows), iters))
        return {r: self._probe[work][r] for r in rows}


def _measure_doc(rows, fp="fp-1"):
    return json.dumps({"rows": {k: {"instr_count": v} for k, v in rows.items()},
                       "meta": {"rustc": "rustc 1.97.0-nightly", "profile_fingerprint": fp}})


def case_79():
    """CodSpeed confirm: run_confirm measures the affected targets on both sides."""
    from aro import confirm as cf
    from aro import spec as specmod
    print("=== case 79: run_confirm with fake measurements ===")
    sp = specmod.from_dict(_spec_dict())
    cfg = cf.config(sp)
    assert cfg.bench_targets == ["transact", "block"] and cfg.control_lanes == ["op_revm"]
    assert cfg.package == "p" and cfg.rounds == 1 and cfg.probe_iters == 8
    assert cf.config(specmod.from_dict(_spec_dict(codspeed_confirm={"enabled": False}))) is None

    probe = {"base": {"transact/a/satin": 1000, "block/t": 5000},
             "cand": {"transact/a/satin": 990, "block/t": 5000}}
    cs = {"base": {"transact/a/satin": 2000, "transact/a/op_revm": 1500},
          "cand": {"transact/a/satin": 1985, "transact/a/op_revm": 1500}}
    seen = []

    def runner_for(work):
        def run(cmd, timeout=None):
            seen.append((work, cmd))
            return _measure_doc(cs[work]), "", 0
        return run

    t = _FakeTarget(sp, probe)
    r = cf.run_confirm(t, "base", "cand", cfg=cfg, measure_runner_for=runner_for,
                       measure_bin="/fake/reporter")
    assert r.status == cf.CONFIRMED, r.reasons
    assert r.targets == ["transact"], r.targets          # only block/t moved? no: transact
    assert [w for w, _ in seen] == ["base", "cand"]
    cmd = seen[0][1]
    assert cmd[:2] == ["/fake/reporter", "measure"] and "--instructions" in cmd
    assert cmd[cmd.index("--bench-target") + 1] == "transact" and "block" not in cmd
    assert cmd[cmd.index("--package") + 1] == "p"
    assert t.icount_calls[0] == ("base", ("block/t", "transact/a/satin"), 8)
    ev = r.to_event()
    assert ev["rows"]["transact/a/satin"]["codspeed_cand"] == 1985
    assert ev["rows"]["block/t"]["probe_delta_pct"] == 0.0

    # A changed build (profile fingerprint differs) is a disagreement.
    def drift_for(work):
        def run(cmd, timeout=None):
            return _measure_doc(cs[work], fp="fp-" + work), "", 0
        return run
    r = cf.run_confirm(_FakeTarget(sp, probe), "base", "cand", cfg=cfg,
                       measure_runner_for=drift_for, measure_bin="/fake/reporter")
    assert r.status == cf.DISAGREED and "fingerprints differ" in r.reasons[-1], r.reasons

    # The instrument cannot answer: errored, with the probe evidence kept.
    def broken_for(work):
        def run(cmd, timeout=None):
            return "", "codspeed: not found", 127
        return run
    r = cf.run_confirm(_FakeTarget(sp, probe), "base", "cand", cfg=cfg,
                       measure_runner_for=broken_for, measure_bin="/fake/reporter")
    assert r.status == cf.ERRORED and "codspeed: not found" in r.reasons[0], r.reasons
    assert r.rows["transact/a/satin"]["probe_cand"] == 990
    r = cf.run_confirm(_FakeTarget(sp, probe, fail_rows=True), "base", "cand", cfg=cfg,
                       measure_runner_for=runner_for, measure_bin="/fake/reporter")
    assert r.status == cf.ERRORED and "row mode" in r.reasons[0]

    # A moved row outside every bench target: all targets are measured.
    probe2 = {"base": {"corpus/x": 100, "transact/a/satin": 1000},
              "cand": {"corpus/x": 90, "transact/a/satin": 1000}}
    seen.clear()
    cf.run_confirm(_FakeTarget(sp, probe2), "base", "cand", cfg=cfg,
                   measure_runner_for=runner_for, measure_bin="/fake/reporter")
    cmd = seen[0][1]
    assert [cmd[i + 1] for i, x in enumerate(cmd) if x == "--bench-target"] == ["transact", "block"]

    # The measure environment: the worktree's own target dir, no ARO profile overrides.
    os.environ["CARGO_PROFILE_RELEASE_DEBUG"] = "2"
    try:
        env = cf._measure_env(_FakeTarget(sp, probe), "cand")
    finally:
        os.environ.pop("CARGO_PROFILE_RELEASE_DEBUG", None)
    assert "CARGO_PROFILE_RELEASE_DEBUG" not in env
    assert env["CARGO_TARGET_DIR"].endswith("cand/codspeed-confirm"), env["CARGO_TARGET_DIR"]
    print("case_79 OK: run_confirm measures the affected targets on both sides")


def case_80():
    """CodSpeed confirm: evaluate() keeps a confirmed accept, freezes anything else."""
    from aro import confirm as cf
    from aro import eval as evalmod
    from aro import spec as specmod
    from aro.types import Candidate, Edit, NoiseFloors, Objective, Patch, Verdict, EvalOutcome
    from tests.common import MockTarget
    print("=== case 80: evaluate() with the confirm layer ===")

    class _Events:
        def __init__(self):
            self.rows = []

        def emit(self, event, **f):
            self.rows.append({"event": event, **f})

    sp_on = specmod.from_dict(_spec_dict())

    class _Target(MockTarget):
        has_differential = True
        differential_required = True

        def __init__(self, spec):
            super().__init__()
            self.spec = spec

    def accepted(*a, **k):
        from aro.types import MetricDelta
        d = MetricDelta("metric/x", 100.0, 95.0, -5.0, -6.0, -4.0, 1.0, True, False)
        return [d], {"improved": True, "regressed": False, "noise_limited": False}

    cand = Candidate(id="c1", hypothesis="h", patch=Patch([Edit("src/opt.rs", "a", "b")]))
    calls = []

    def fake_confirm(result):
        def run(target, base, work, *, cfg):
            calls.append((base, cfg.rounds))
            return result
        return run

    orig_sig, orig_run = evalmod._significance, cf.run_confirm
    evalmod._significance = accepted
    try:
        # Confirmed: the accept stands, with a note and the evidence event.
        cf.run_confirm = fake_confirm(cf.ConfirmResult(
            status=cf.CONFIRMED, claimed=["transact/a/satin"], targets=["transact"],
            rows={"transact/a/satin": {"probe_base": 1, "probe_cand": 0.9}}))
        events = _Events()
        out = evalmod.evaluate(_Target(sp_on), "base", Patch([]), cand, 2, NoiseFloors(),
                               [Objective("metric/x")], events=events)
        assert out.verdict == Verdict.ACCEPTED, out.verdict
        assert any("codspeed confirm" in n for n in out.notes)
        conf = [r for r in events.rows if r["event"] == "codspeed_confirm"]
        assert conf and conf[0]["status"] == "confirmed" and conf[0]["candidate"] == "c1"
        assert conf[0]["inner_verdict"] == "accepted"

        # Disagreed: frozen as codspeed-unconfirmed, deltas and both measurements kept.
        cf.run_confirm = fake_confirm(cf.ConfirmResult(
            status=cf.DISAGREED, reasons=["claimed row transact/a/satin: probe Δ-2% but CodSpeed Δ+0.1%"],
            claimed=["transact/a/satin"], targets=["transact"],
            rows={"transact/a/satin": {"probe_base": 1000, "probe_cand": 980,
                                       "codspeed_base": 2000, "codspeed_cand": 2002}}))
        events = _Events()
        out = evalmod.evaluate(_Target(sp_on), "base", Patch([]), cand, 2, NoiseFloors(),
                               [Objective("metric/x")], events=events)
        assert out.verdict == Verdict.CODSPEED_UNCONFIRMED, out.verdict
        assert out.deltas and out.deltas[0].delta_pct == -5.0
        assert any("frozen for review" in n for n in out.notes)
        conf = [r for r in events.rows if r["event"] == "codspeed_confirm"][0]
        assert conf["rows"]["transact/a/satin"]["codspeed_cand"] == 2002
        gate = [r for r in events.rows if r["event"] == "gate"
                and r.get("gate") == "codspeed_confirm"][0]
        assert gate["status"] == "disagreed" and "CodSpeed" in gate["detail"], gate
        from aro.types import is_accept_verdict
        assert not is_accept_verdict(out.verdict), "an unconfirmed candidate never folds"

        # Errored: frozen too (never silently accepted).
        cf.run_confirm = fake_confirm(cf.ConfirmResult(status=cf.ERRORED,
                                                       reasons=["confirm could not measure: x"]))
        out = evalmod.evaluate(_Target(sp_on), "base", Patch([]), cand, 2, NoiseFloors(),
                               [Objective("metric/x")], events=_Events())
        assert out.verdict == Verdict.CODSPEED_UNCONFIRMED

        # Off (no codspeed_confirm), or not an accept: the confirm never runs.
        calls.clear()
        d = _spec_dict()
        d.pop("codspeed_confirm")
        out = evalmod.evaluate(_Target(specmod.from_dict(d)), "base", Patch([]), cand, 2,
                               NoiseFloors(), [Objective("metric/x")], events=_Events())
        assert out.verdict == Verdict.ACCEPTED and not calls
        res = evalmod._codspeed_confirm(_Target(sp_on), "b", "w", cand,
                                        EvalOutcome("c1", Verdict.WITHIN_NOISE, [], []),
                                        lambda *a, **k: None)
        assert res.verdict == Verdict.WITHIN_NOISE and not calls
    finally:
        evalmod._significance, cf.run_confirm = orig_sig, orig_run
    print("case_80 OK: evaluate() keeps a confirmed accept, freezes anything else")


def case_81():
    """CodSpeed confirm: the manifest stamps entries, blocks unconfirmed ones, lists the frozen."""
    from aro import manifest as mf
    from aro import patchfile
    from aro.types import Edit, Patch
    print("=== case 81: manifest confirm stamps and frozen list ===")
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp)
        (run / "a1/patches").mkdir(parents=True)
        for cid in ("c1", "c2", "c3"):
            (run / f"a1/patches/{cid}.txt").write_text(
                patchfile.dump(Patch([Edit("src/x.rs", f"{cid}a", f"{cid}b")])))
        ev = [
            {"event": "run_started", "run_id": "r1", "target": "s", "baseline_ref": "abc",
             "codspeed_confirm": True},
            {"event": "attempt_started", "attempt": 1, "fn": "hot", "regime": "byte-identical"},
            {"event": "candidate_proposed", "attempt": 1, "id": "c1", "hypothesis": "one"},
            {"event": "codspeed_confirm", "attempt": 1, "candidate": "c1", "status": "confirmed",
             "claimed": ["transact/a/satin"], "targets": ["transact"], "rows": {}},
            {"event": "candidate_verdict", "attempt": 1, "id": "c1", "verdict": "accepted",
             "deltas": [{"metric": "m", "delta_pct": -1.0, "improved": True}]},
            {"event": "critic", "attempt": 1, "id": "c1", "verdict": "pass"},
            {"event": "baseline_advanced", "attempt": 1, "by": "c1"},
            # c2 folded by a path that never confirmed it (e.g. a micro-bench fold).
            {"event": "candidate_verdict", "attempt": 1, "id": "c2", "verdict": "accepted",
             "deltas": [{"metric": "m", "delta_pct": -1.0, "improved": True}]},
            {"event": "critic", "attempt": 1, "id": "c2", "verdict": "pass"},
            {"event": "baseline_advanced", "attempt": 1, "by": "c2"},
            # c3 accepted by the inner loop, not confirmed: frozen.
            {"event": "codspeed_confirm", "attempt": 1, "candidate": "c3", "status": "disagreed",
             "inner_verdict": "accepted-ir", "reasons": ["row block/t regresses"],
             "claimed": ["transact/a/satin"], "targets": ["transact", "block"], "rows": {}},
            {"event": "candidate_verdict", "attempt": 1, "id": "c3",
             "verdict": "codspeed-unconfirmed", "deltas": []},
        ]
        for i, e in enumerate(ev):
            e.setdefault("run_id", "r1")
            e["seq"] = i + 1
        (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev))
        m = mf.build_manifest(run)
        by = {e["id"]: e for e in m["accepted"]}
        assert by["c1"]["codspeed_confirm"]["status"] == "confirmed"
        assert by["c1"]["mergeable"], by["c1"].get("merge_reasons")
        assert by["c2"]["codspeed_confirm"] == {"required": True, "status": "missing",
                                                "claimed": [], "targets": []}
        assert not by["c2"]["mergeable"]
        dec = mf.resolve_mergeability(by["c2"], regime="byte-identical", critic_verdict="pass",
                                      terminal_required=False)
        assert dec.reasons == ["codspeed confirm: missing"], dec.reasons
        assert [f["id"] for f in m["frozen"]] == ["c3"]
        fz = m["frozen"][0]
        assert fz["patch_path"] == "a1/patches/c3.txt" and fz["inner_verdict"] == "accepted-ir"
        assert fz["reasons"] == ["row block/t regresses"] and fz["fn"] == "hot"

    # The resolver reads the stamp from the entry alone (every resolve path agrees).
    dec = mf.resolve_mergeability({"codspeed_confirm": {"required": True, "status": "errored"}},
                                  regime="byte-identical", critic_verdict="pass",
                                  terminal_required=False)
    assert not dec.mergeable and dec.reasons == ["codspeed confirm: errored"], dec.reasons
    dec = mf.resolve_mergeability({"codspeed_confirm": {"required": False, "status": "missing"}},
                                  regime="byte-identical", critic_verdict="pass",
                                  terminal_required=False)
    assert dec.mergeable
    print("case_81 OK: manifest confirm stamps and frozen list")


_FAKE_VALGRIND = r'''#!{python}
import os, subprocess, sys
args = sys.argv[1:]
out = next(a.split("=", 1)[1] for a in args if a.startswith("--callgrind-out-file="))
assert "--collect-atstart=no" in args, args
assert "--toggle-collect=aro_probe_measured" in args, args
assert os.environ.get("RAYON_NUM_THREADS") == "1"
binary = args[-1]
p = subprocess.run([binary], capture_output=True, text=True)
sys.stdout.write(p.stdout)
row, iters = os.environ["ARO_PROBE_ROW"], int(os.environ["ARO_PROBE_ITERS"])
ir = {{"transact/a/satin": 1200, "block/t": 0}}[row] * iters
open(out, "w").write("events: Ir Dr\ntotals: %d 5\n" % ir)
'''

_FAKE_PROBE = r'''#!{python}
import os
if os.environ.get("ARO_PROBE_LIST"):
    print("ROWS transact/a/satin block/t")
elif os.environ.get("ARO_PROBE_ROW"):
    print("ROWRAN %s %s" % (os.environ["ARO_PROBE_ROW"], os.environ["ARO_PROBE_ITERS"]))
'''


def case_82():
    """CodSpeed confirm: SpecTarget.icount_rows collects inside aro_probe_measured per row."""
    from aro import spec as specmod
    from aro.target import SpecTarget
    print("=== case 82: per-row icount through a fake valgrind ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        bindir = tmp / "bin"
        bindir.mkdir()
        vg = bindir / "valgrind"
        vg.write_text(_FAKE_VALGRIND.format(python=sys.executable))
        vg.chmod(0o755)
        probe = tmp / "probe"
        probe.write_text(_FAKE_PROBE.format(python=sys.executable))
        probe.chmod(0o755)
        repo = tmp / "repo"
        repo.mkdir()
        d = _spec_dict()
        d["target_repo"]["path"] = str(repo)
        t = SpecTarget(specmod.from_dict(d))
        t.build_example = lambda work, example=None: probe
        t._profile_fidelity = lambda work: "fp"
        work = tmp / "wt"
        work.mkdir()
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"
        try:
            rows = t.probe_rows(work)
            assert rows == ["transact/a/satin", "block/t"], rows
            got = t.icount_rows(work, ["transact/a/satin"], iters=4)
            assert got == {"transact/a/satin": 1200.0}, got
            # A row whose toggle collected nothing (stripped symbol) is an error, not a zero.
            try:
                t.icount_rows(work, ["block/t"], iters=2)
            except RuntimeError as e:
                assert "collected nothing inside aro_probe_measured" in str(e), e
            else:
                raise AssertionError("a zero per-row count passed")
        finally:
            os.environ["PATH"] = old_path
        # A probe without a row mode is named as such.
        silent = tmp / "silent"
        silent.write_text(f"#!{sys.executable}\nprint('BENCH 1 2 3')\n")
        silent.chmod(0o755)
        t.build_example = lambda work, example=None: silent
        try:
            t.probe_rows(work)
        except RuntimeError as e:
            assert "no row mode" in str(e), e
        else:
            raise AssertionError("a probe without a row mode listed rows")
    print("case_82 OK: per-row icount collects inside aro_probe_measured")
