from __future__ import annotations

import json
import tempfile
from pathlib import Path


def case_39():
    """T17: aro reverify — re-adjudicate frozen manifest candidates.

    Hermetic — real filesystem worktrees + SEARCH/REPLACE apply; injected
    gate failures via the target; never spawns cargo/git.
    (a) three-entry replay: entry 2 fails a gate → reverted; entry 3 gated
        on top of entry 1 → pass, fail, pass; preflight pass in JSON
    (b) entry 1 patch corrupted → unappliable; entry 2 (depends on 1) also
        unappliable
    (c) test_full declared + failing → reverify-fail (failing_gate=test_full);
        not declared → gates has no test_full key
    (d) --apply stamps reverify, forces mergeable=false on failures, never
        flips false→true
    (e) reverify.json round-trips through json.load
    (f) pre-flight fail on pristine build → empty entries, preflight fail,
        manifest untouched even with --apply; CLI exits non-zero
    (g) pre-flight pass → header preflight pass; candidates gated as today
    """
    print("=== case 39: aro reverify (gate-hardening re-adjudication) ===")
    import shutil
    from types import SimpleNamespace
    from aro import reverify as _rv
    from aro import patchfile as _pf
    from aro.eval import run_correctness_gates as _rcg

    SRC = "src/lib.rs"

    class _ReverifyTarget:
        """File-backed mock: real SEARCH/REPLACE on temp dirs (no cargo/git)."""
        name = "reverify-mock"
        differential_required = False
        has_differential = True

        def __init__(self, *, base_src="A", fail_diff_when=None, fail_test_when=None,
                     fail_build_when=None):
            # fail_*_when: callable(work_src_content) -> bool
            self._base_src = base_src
            self.fail_diff_when = fail_diff_when
            self.fail_test_when = fail_test_when
            self.fail_build_when = fail_build_when
            self._tick = 0
            self._owned = []

        def make_worktree(self, tag):
            self._tick += 1
            d = Path(tempfile.mkdtemp(prefix=f"rv-{tag}-{self._tick}-"))
            (d / "src").mkdir(parents=True)
            (d / SRC).write_text(self._base_src)
            self._owned.append(d)
            return d

        def remove_worktree(self, work):
            p = Path(work)
            shutil.rmtree(p, ignore_errors=True)
            if p in self._owned:
                self._owned.remove(p)

        def apply(self, patch, work):
            for e in patch.edits:
                f = Path(work) / e.path
                content = f.read_text()
                n = content.count(e.search)
                if n != 1:
                    what = "not found" if n == 0 else f"found {n}x"
                    raise RuntimeError(f"search text {what} in {e.path}")
                i = content.find(e.search)
                f.write_text(content[:i] + e.replace + content[i + len(e.search):])

        def build(self, work):
            src = (Path(work) / SRC).read_text()
            if self.fail_build_when and self.fail_build_when(src):
                raise RuntimeError("build exploded on " + src)
            return "Compiling mock"

        def test(self, work):
            src = (Path(work) / SRC).read_text()
            if self.fail_test_when and self.fail_test_when(src):
                raise RuntimeError("tests failed on " + src)
            return 3

        def differential(self, work, baseline):
            src = (Path(work) / SRC).read_text()
            if self.fail_diff_when and self.fail_diff_when(src):
                return False
            return True

    def _write_manifest(d, entries, *, mergeable_flags=None):
        from aro.types import Edit as _Ed, Patch as _Pa
        accepted = []
        for i, (cid, fn, search, replace) in enumerate(entries, 1):
            adir = d / f"a{i}" / "patches"
            adir.mkdir(parents=True, exist_ok=True)
            text = _pf.dump(_Pa(edits=[_Ed(SRC, search, replace)]))
            (adir / f"{cid}.txt").write_text(text)
            mflag = True
            if mergeable_flags is not None:
                mflag = mergeable_flags[i - 1]
            accepted.append({
                "order": i, "attempt": f"a{i}", "id": cid, "fn": fn,
                "files": [SRC], "delta_pct": -1.0, "regime": "byte-identical",
                "critic_verdict": "pass", "mergeable": mflag,
                "hypothesis": f"h-{cid}",
                "patch_path": f"a{i}/patches/{cid}.txt",
            })
        man = {
            "spec": "reverify-demo", "baseline_ref": "abc123",
            "accepted": accepted, "files_touched": [SRC], "notes": "",
        }
        (d / "manifest.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=1) + "\n")
        return man

    def _spec(**raw_extra):
        raw = dict(raw_extra)
        return SimpleNamespace(
            name="reverify-demo",
            baseline_ref="abc123",
            build=["true"], test=["true"],
            differential={"example": "semantics_diff", "pkg": "p",
                          "probe": "probes/x.rs", "prefix": "DIFF"},
            raw=raw,
        )

    # ---- (a) three-entry: entry 2 fails differential → reverted; 3 on top of 1
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # entry1: A→AB; entry2: AB→ABX (fails gate); entry3: AB→ABY (needs 1)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABX"),
            ("c3", "fn3", "AB", "ABY"),
        ])
        # Fail differential only when content is ABX (entry 2 applied)
        tgt = _ReverifyTarget(
            base_src="A",
            fail_diff_when=lambda s: s == "ABX")
        doc = _rv.reverify(_spec(), d, target=tgt)
        vs = [e["verdict"] for e in doc["entries"]]
        assert vs == ["reverify-pass", "reverify-fail", "reverify-pass"], vs
        assert doc["preflight"] == "pass"
        assert "detail" not in doc  # detail only on preflight fail
        assert doc["entries"][1]["failing_gate"] == "differential"
        assert doc["entries"][1]["gates"].get("differential") == "fail"
        assert doc["entries"][0]["gates"].get("build") == "ok"
        assert "test_full" not in doc["entries"][0]["gates"]
        assert doc["probe"] == "semantics_diff"
        assert doc["spec"] == "reverify-demo"
        assert "build" in doc["gate_config_summary"]
        # round-trip reverify.json
        loaded = json.loads((d / "reverify.json").read_text())
        assert loaded["entries"][1]["verdict"] == "reverify-fail"
        assert loaded["preflight"] == "pass"
        assert loaded == doc
    print("#50a OK: three-entry replay pass/fail/pass with entry-2 revert")

    # ---- (b) entry 1 corrupted → unappliable; entry 2 depends on 1 → unappliable
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABY"),
        ])
        # Corrupt entry 1's patch so SEARCH never matches baseline "A"
        bad = d / "a1" / "patches" / "c1.txt"
        bad.write_text(
            "--- edit 1 ---\npath: src/lib.rs\n<<<<<<< SEARCH\nNOT_A\n"
            "=======\nAB\n>>>>>>> REPLACE\n")
        tgt = _ReverifyTarget(base_src="A")
        doc = _rv.reverify(_spec(), d, target=tgt)
        vs = [e["verdict"] for e in doc["entries"]]
        assert vs == ["unappliable", "unappliable"], vs
        assert "apply failed" in doc["entries"][0]["detail"]
    print("#50b OK: corrupted entry 1 → both unappliable (compounding)")

    # ---- (c) test_full declared + failing; absent → no key
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [("c1", "fn1", "A", "AB")])
        tgt = _ReverifyTarget(base_src="A")
        tf_calls = []

        def _tf_fail(cmd, *, cwd, timeout=None):
            tf_calls.append((list(cmd), str(cwd), timeout))
            return "FAIL: semantics\n" * 80, "error\n", 1

        sp = _spec(correctness_oracle={
            "build": ["true"], "test": ["true"],
            "test_full": ["cargo", "test", "--release", "-p", "mega-evm"],
        })
        doc = _rv.reverify(sp, d, target=tgt, test_full_runner=_tf_fail)
        assert doc["entries"][0]["verdict"] == "reverify-fail"
        assert doc["entries"][0]["failing_gate"] == "test_full"
        assert doc["entries"][0]["gates"].get("test_full") == "fail"
        assert doc["entries"][0]["gates"].get("build") == "ok"
        assert doc["entries"][0]["gates"].get("test") == "ok"
        assert "differential" not in doc["entries"][0]["gates"]  # fail-fast
        assert len(tf_calls) == 1
        assert "FAIL: semantics" in doc["entries"][0]["detail"]
        assert "test_full" in doc["gate_config_summary"]
    # absent test_full: gates dict has no test_full
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [("c1", "fn1", "A", "AB")])
        doc = _rv.reverify(_spec(), d, target=_ReverifyTarget(base_src="A"))
        assert doc["entries"][0]["verdict"] == "reverify-pass"
        assert "test_full" not in doc["entries"][0]["gates"]
        assert "test_full" not in doc["gate_config_summary"]
    print("#50c OK: test_full fail-fast + absent omits key")

    # ---- (d) --apply stamps; forces mergeable=false; never false→true
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # entry1 mergeable false (human already demoted); entry2 fails gate
        # (was mergeable true); entry3 passes (was mergeable true)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABX"),
            ("c3", "fn3", "AB", "ABY"),
        ], mergeable_flags=[False, True, True])
        tgt = _ReverifyTarget(
            base_src="A",
            fail_diff_when=lambda s: s == "ABX")
        doc = _rv.reverify(_spec(), d, target=tgt, apply=True)
        man = json.loads((d / "manifest.json").read_text())
        acc = man["accepted"]
        assert acc[0]["reverify"]["verdict"] == "reverify-pass"
        assert acc[0]["mergeable"] is False  # NEVER promoted
        assert acc[1]["reverify"]["verdict"] == "reverify-fail"
        assert acc[1]["reverify"]["failing_gate"] == "differential"
        assert acc[1]["mergeable"] is False  # forced false
        assert acc[2]["reverify"]["verdict"] == "reverify-pass"
        assert acc[2]["mergeable"] is True   # left alone on pass
        assert "failing_gate" not in acc[0]["reverify"]
        assert "failing_gate" not in acc[2]["reverify"]
    print("#50d OK: --apply stamps; never promotes mergeable; fails demoted")

    # ---- (e) shared run_correctness_gates unit + CLI parse seam
    class _T:
        differential_required = False
        has_differential = True

        def build(self, w):
            return "ok"

        def test(self, w):
            return 1

        def differential(self, w, b):
            return True

    g = _rcg(_T(), "work", "base")
    assert g["ok"] and g["failing_gate"] is None
    assert set(g["gates"]) == {"build", "test", "differential"}

    # CLI surface: flags exist, help mentions no-auto-promotion
    from aro.cli import build_parser
    p = build_parser()
    a = p.parse_args(["reverify", "--spec", "t.json", "--out", "/tmp/x",
                      "--orders", "1,3", "--apply"])
    assert a.cmd == "reverify" and a.spec == "t.json" and a.out == "/tmp/x"
    assert a.orders == "1,3" and a.apply is True
    sub = None
    for action in p._subparsers._actions:
        if getattr(action, "choices", None) and "reverify" in (action.choices or {}):
            sub = action.choices["reverify"]
            break
    assert sub is not None
    h = sub.format_help()
    assert "NEVER" in h or "never" in h.lower()
    assert "human" in h.lower()
    print("#50e OK: run_correctness_gates + CLI --orders/--apply/help")

    # ---- (f) pre-flight fail: pristine build broken → no entries judged;
    #          --apply must not touch manifest; CLI exits non-zero
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABY"),
        ], mergeable_flags=[True, True])
        man_before = (d / "manifest.json").read_text()
        # Always fail build — including on the pristine baseline "A".
        tgt = _ReverifyTarget(base_src="A", fail_build_when=lambda s: True)
        doc = _rv.reverify(_spec(), d, target=tgt, apply=True)
        assert doc["preflight"] == "fail", doc
        assert doc["entries"] == [], doc["entries"]
        assert doc.get("detail"), "preflight fail must carry a detail tail"
        assert "build failed" in doc["detail"]
        assert (d / "manifest.json").read_text() == man_before  # untouched
        loaded = json.loads((d / "reverify.json").read_text())
        assert loaded["preflight"] == "fail"
        assert loaded["entries"] == []
        assert loaded["detail"] == doc["detail"]
        assert loaded.get("spec") == "reverify-demo"
        # CLI surface: preflight fail → SystemExit(1) + loud diagnosis
        from types import SimpleNamespace as _SN
        import io
        from contextlib import redirect_stderr
        import aro.spec as _specmod
        called = {}

        def _fake_reverify(*a, **k):
            called["yes"] = True
            return doc

        _real_rv = _rv.reverify
        _real_load = _specmod.load
        _rv.reverify = _fake_reverify
        _specmod.load = lambda p: _spec()
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                try:
                    _rv.cli(_SN(spec="t.json", out=str(d), orders=None,
                                apply=False))
                    raise AssertionError("cli should SystemExit(1) on preflight fail")
                except SystemExit as se:
                    assert se.code == 1, se.code
            err_text = err.getvalue()
            assert "UNPATCHED baseline" in err_text
            assert "no candidate was judged" in err_text
            assert called.get("yes")
        finally:
            _rv.reverify = _real_rv
            _specmod.load = _real_load
    print("#50f OK: pre-flight fail → empty entries, manifest untouched, exit 1")

    # ---- (g) pre-flight pass → preflight field present; candidates gated
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [("c1", "fn1", "A", "AB")])
        doc = _rv.reverify(_spec(), d, target=_ReverifyTarget(base_src="A"))
        assert doc["preflight"] == "pass"
        assert doc["entries"][0]["verdict"] == "reverify-pass"
        assert "detail" not in doc
        loaded = json.loads((d / "reverify.json").read_text())
        assert loaded["preflight"] == "pass"
        assert len(loaded["entries"]) == 1
    # pre-flight test failure path (not just build)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [("c1", "fn1", "A", "AB")])
        man_before = (d / "manifest.json").read_text()
        tgt = _ReverifyTarget(base_src="A", fail_test_when=lambda s: True)
        doc = _rv.reverify(_spec(), d, target=tgt, apply=True)
        assert doc["preflight"] == "fail"
        assert doc["entries"] == []
        assert "tests failed" in doc["detail"]
        assert (d / "manifest.json").read_text() == man_before
    print("#50g OK: pre-flight pass + test-fail preflight path")

    # ---- (h) T24: acceptance chain — present fields, corrupted parent, legacy
    def _write_manifest_with_chain(d, entries, *, corrupt_parent_at=None):
        """Like _write_manifest but stamps acceptance_seq + parent (new shape)."""
        from aro.types import Edit as _Ed, Patch as _Pa
        accepted = []
        prev = "abc123"
        for i, (cid, fn, search, replace) in enumerate(entries, 1):
            adir = d / f"a{i}" / "patches"
            adir.mkdir(parents=True, exist_ok=True)
            text = _pf.dump(_Pa(edits=[_Ed(SRC, search, replace)]))
            (adir / f"{cid}.txt").write_text(text)
            parent = prev
            if corrupt_parent_at is not None and i == corrupt_parent_at:
                parent = "CORRUPTED_PARENT"
            accepted.append({
                "order": i, "attempt": f"a{i}", "id": cid, "fn": fn,
                "files": [SRC], "delta_pct": -1.0, "regime": "byte-identical",
                "critic_verdict": "pass", "mergeable": True,
                "hypothesis": f"h-{cid}",
                "patch_path": f"a{i}/patches/{cid}.txt",
                "acceptance_seq": i * 10,
                "parent": parent,
            })
            prev = cid
        man = {
            "spec": "reverify-demo", "baseline_ref": "abc123",
            "accepted": accepted, "files_touched": [SRC], "notes": "",
        }
        (d / "manifest.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=1) + "\n")
        return man

    # (h1) chain fields present → three-entry replay still pass/fail/pass
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest_with_chain(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABX"),
            ("c3", "fn3", "AB", "ABY"),
        ])
        tgt = _ReverifyTarget(
            base_src="A",
            fail_diff_when=lambda s: s == "ABX")
        doc = _rv.reverify(_spec(), d, target=tgt)
        vs = [e["verdict"] for e in doc["entries"]]
        assert vs == ["reverify-pass", "reverify-fail", "reverify-pass"], vs
        assert doc["preflight"] == "pass"
    print("#50h1 OK: reverify with acceptance chain fields → pass/fail/pass")

    # (h2) corrupted parent → hard error BEFORE any worktree / gate work
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest_with_chain(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABY"),
        ], corrupt_parent_at=2)
        worktree_calls = []
        gate_calls = []

        class _CountTarget(_ReverifyTarget):
            def make_worktree(self, tag):
                worktree_calls.append(tag)
                return super().make_worktree(tag)

        # reverify binds run_correctness_gates at import time — patch that name.
        _real_gates = _rv.run_correctness_gates

        def _count_gates(*a, **k):
            gate_calls.append(1)
            return _real_gates(*a, **k)

        _rv.run_correctness_gates = _count_gates
        try:
            try:
                _rv.reverify(_spec(), d, target=_CountTarget(base_src="A"))
                raise AssertionError(
                    "expected ValueError for corrupted acceptance chain")
            except ValueError as err:
                msg = str(err)
                assert "order=2" in msg or "c2" in msg, msg
        finally:
            _rv.run_correctness_gates = _real_gates
        assert worktree_calls == [], worktree_calls
        assert gate_calls == [], gate_calls
    print("#50h2 OK: corrupted parent → hard error, zero worktree/gate calls")

    # (h3) no chain fields → legacy order replay + one-line notice
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABY"),
        ])
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            doc = _rv.reverify(
                _spec(), d, target=_ReverifyTarget(base_src="A"))
        out = buf.getvalue()
        assert _rv.LEGACY_CHAIN_NOTICE in out, out
        assert [e["verdict"] for e in doc["entries"]] == [
            "reverify-pass", "reverify-pass"]
    print("#50h3 OK: legacy manifest → notice + order-based replay")
    print("case 39 OK")

