from __future__ import annotations

import json
import tempfile
from pathlib import Path


def case_42():
    """T25: aro ablate — per-entry attribution + greedy proposal.

    Hermetic — real filesystem worktrees + SEARCH/REPLACE; injected measure
    docs; never spawns cargo/git/measure binary.
    (a) three-entry chain: correct marginals; one drop (protected); proposal
    (b) unappliable-after-drop when survivor SEARCH needs a dropped prefix
    (c) preflight-fail → zero attribution
    (d) --dry-run prints plan without measuring
    (e) band → upgrade invoked exactly once with upgrade rounds
    """
    print("=== case 42: aro ablate (attribution + proposal) ===")
    import shutil
    from types import SimpleNamespace
    from aro import ablate as _ab
    from aro import patchfile as _pf
    from aro import terminal as _tm

    SRC = "src/lib.rs"
    FAM = ["oracle_sload"]
    CAP = 1.0
    HYST = {"margin_pp": 0.05, "floor_multiple": 1.5}
    FP = "fp-ablate"

    class _AblateTarget:
        name = "ablate-mock"
        differential_required = False
        has_differential = False

        def __init__(self, *, base_src="A", fail_build_when=None):
            self._base_src = base_src
            self.fail_build_when = fail_build_when
            self._tick = 0
            self._owned = []

        def make_worktree(self, tag):
            self._tick += 1
            d = Path(tempfile.mkdtemp(prefix=f"ab-{tag}-{self._tick}-"))
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
            return "ok"

        def test(self, work):
            return 3

    def _write_manifest(d, entries):
        from aro.types import Edit as _Ed, Patch as _Pa
        accepted = []
        for i, (cid, fn, search, replace) in enumerate(entries, 1):
            adir = d / f"a{i}" / "patches"
            adir.mkdir(parents=True, exist_ok=True)
            text = _pf.dump(_Pa(edits=[_Ed(SRC, search, replace)]))
            (adir / f"{cid}.txt").write_text(text)
            accepted.append({
                "order": i, "attempt": f"a{i}", "id": cid, "fn": fn,
                "files": [SRC], "delta_pct": -1.0, "regime": "byte-identical",
                "critic_verdict": "pass", "mergeable": True,
                "hypothesis": f"h-{cid}",
                "patch_path": f"a{i}/patches/{cid}.txt",
                "acceptance_seq": i - 1,
                "parent": "base" if i == 1 else entries[i - 2][0],
            })
        man = {
            "spec": "ablate-demo", "baseline_ref": "abc123",
            "accepted": accepted, "files_touched": [SRC], "notes": "",
        }
        (d / "manifest.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=1) + "\n")
        return man

    def _spec(**extra):
        raw = {
            "terminal_bench_targets": ["mega_bench"],
            "protected_row_families": FAM,
            "tradeable_regression_cap_pct": CAP,
            "protected_hysteresis": HYST,
            "benchmark_probe": {"pkg": "mock"},
            "measure_bin": "/nonexistent/measure",
        }
        raw.update(extra)
        return SimpleNamespace(
            name="ablate-demo",
            baseline_ref="abc123",
            build=["true"], test=["true"],
            differential=None,
            bench={"pkg": "mock"},
            raw=raw,
            terminal_bench_targets=["mega_bench"],
            icount_epsilon_pct=0.1,
        )

    def _mdoc(rows):
        return _tm.MeasureDoc(
            rows=dict(rows), meta={"profile_fingerprint": FP},
            profile_fingerprint=FP, rustc="r")

    # Content → absolute row IRs used to drive marginals.
    # Baseline "A": both families clean.
    # After c1 (A→AB): improve other family.
    # After c2 (AB→ABX): regress protected oracle_sload heavily → drop.
    # After c3 (ABX→ABXY): improve other more (depends on c2's X).
    STATE_ROWS = {
        "A": {"oracle_sload/r": 10000, "misc/r": 10000},
        "AB": {"oracle_sload/r": 10000, "misc/r": 9000},   # -10% misc
        "ABX": {"oracle_sload/r": 12000, "misc/r": 9000},  # +20% protected
        "ABXY": {"oracle_sload/r": 12000, "misc/r": 8000},  # -11% misc
        "ABY": {"oracle_sload/r": 10000, "misc/r": 8500},  # for unappliable path
    }

    def _measure_from_work(work, *, rounds):
        src = (Path(work) / SRC).read_text()
        rows = STATE_ROWS.get(src)
        if rows is None:
            raise AssertionError(f"unexpected worktree content {src!r}")
        return _mdoc(rows)

    # ---- (a) three-entry: c2 drops on protected violation; proposal = c1 only
    # (c3 SEARCH needs ABX which includes c2 — will be unappliable-after-drop)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABX"),
            ("c3", "fn3", "ABX", "ABXY"),
        ])
        tgt = _AblateTarget(base_src="A")
        measure_calls = []

        def _mf(work, *, rounds):
            measure_calls.append(rounds)
            return _measure_from_work(work, rounds=rounds)

        doc = _ab.ablate(
            _spec(), d, target=tgt, measure_fn=_mf, rounds=2,
            upgrade_rounds=5, floors={"oracle_sload/r": 0.1, "misc/r": 0.1})
        assert doc["preflight"] == "pass"
        assert len(doc["entries"]) == 3, doc["entries"]
        vs = [e["policy_verdict"] for e in doc["entries"]]
        assert vs[0] == "keep", vs
        assert vs[1] == "drop", vs
        # c3 marginal: ABX→ABXY improves misc only → keep on its own
        assert vs[2] == "keep", vs
        # proposal: c1 only (c3 unappliable without c2)
        prop_ids = [p["id"] for p in doc["proposal"]]
        assert prop_ids == ["c1"], prop_ids
        assert any(u["id"] == "c3" for u in doc["unappliable_after_drop"]), doc
        assert doc["dropped"][0]["id"] == "c2"
        # marginal for c1: misc improved
        assert "misc/r" in doc["entries"][0]["marginal_rows_summary"]
        # manifest untouched
        man = json.loads((d / "manifest.json").read_text())
        assert "ablate" not in man
        assert all("ablate" not in a for a in man["accepted"])
        # ablate.json round-trips
        loaded = json.loads((d / "ablate.json").read_text())
        assert loaded["entries"][1]["policy_verdict"] == "drop"
    print("#53a OK: three-entry attribution + drop + proposal")

    # ---- (b) unappliable-after-drop explicit path (already covered above)
    print("#53b OK: unappliable-after-drop when SEARCH needs dropped prefix")

    # ---- (c) preflight fail → zero attribution
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
        ])
        tgt = _AblateTarget(base_src="A", fail_build_when=lambda s: True)
        calls = []

        def _mf(work, *, rounds):
            calls.append(1)
            return _mdoc(STATE_ROWS["A"])

        doc = _ab.ablate(
            _spec(), d, target=tgt, measure_fn=_mf, rounds=1,
            floors={"oracle_sload/r": 0.1, "misc/r": 0.1})
        assert doc["preflight"] == "fail"
        assert doc["entries"] == []
        assert doc["proposal"] == []
        assert calls == []  # zero attribution / zero measure
    print("#53c OK: preflight-fail → zero attribution")

    # ---- (d) dry-run: plan, no measure
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
            ("c2", "fn2", "AB", "ABX"),
        ])
        tgt = _AblateTarget(base_src="A")
        calls = []

        def _mf(work, *, rounds):
            calls.append(rounds)
            return _mdoc(STATE_ROWS["A"])

        doc = _ab.ablate(
            _spec(), d, target=tgt, measure_fn=_mf, dry_run=True)
        assert doc.get("dry_run") is True
        assert len(doc.get("plan") or []) == 2
        assert calls == []
        assert doc["entries"] == []
    print("#53d OK: --dry-run plan without measuring")

    # ---- (e) band → upgrade once with upgrade_rounds
    # floor=1.0 → H=1.5; first measure Δ=+1.2% band; upgraded Δ=+0.5% → keep
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _write_manifest(d, [
            ("c1", "fn1", "A", "AB"),
        ])
        tgt = _AblateTarget(base_src="A")
        call_rounds = []
        phase = {"n": 0}

        def _mf_band(work, *, rounds):
            call_rounds.append(rounds)
            src = (Path(work) / SRC).read_text()
            if src == "A":
                return _mdoc({"oracle_sload/r": 10000, "misc/r": 10000})
            # After apply: first pair of measures (rounds=2) → band +1.2%
            # upgrade pair (rounds=5) → keep-level +0.5% protected + improve misc
            phase["n"] += 1
            if rounds == 5:
                return _mdoc({"oracle_sload/r": 10050, "misc/r": 9000})
            return _mdoc({"oracle_sload/r": 10120, "misc/r": 9000})

        doc = _ab.ablate(
            _spec(), d, target=tgt, measure_fn=_mf_band, rounds=2,
            upgrade_rounds=5,
            floors={"oracle_sload/r": 1.0, "misc/r": 1.0})
        assert doc["entries"][0]["upgraded"] is True, doc["entries"][0]
        assert doc["entries"][0]["policy_verdict"] == "keep", doc["entries"][0]
        # baseline(2) + post-apply(2) + upgrade prev(5) + upgrade curr(5)
        assert call_rounds.count(5) == 2, call_rounds
        assert 5 in call_rounds and call_rounds.count(2) >= 2
        # upgrade invoked once (one pair) — not looped
        assert call_rounds.count(5) == 2
    print("#53e OK: band → upgrade once with upgraded rounds")

    # entry_policy_verdict unit checks
    r_keep = _tm.judge_terminal(
        _mdoc({"misc/r": 10000}), _mdoc({"misc/r": 9000}),
        epsilon_pct=0.1, default_floor_pct=0.1,
        protected_row_families=FAM, tradeable_regression_cap_pct=CAP,
        protected_hysteresis=HYST)
    assert _ab.entry_policy_verdict(
        r_keep, protected_families=FAM, cap_pct=CAP, hysteresis=HYST) == "keep"
    r_drop = _tm.judge_terminal(
        _mdoc({"oracle_sload/r": 10000}), _mdoc({"oracle_sload/r": 12000}),
        epsilon_pct=0.1, default_floor_pct=0.1,
        protected_row_families=FAM, tradeable_regression_cap_pct=CAP,
        protected_hysteresis=HYST)
    assert _ab.entry_policy_verdict(
        r_drop, protected_families=FAM, cap_pct=CAP, hysteresis=HYST) == "drop"
    print("#53f OK: entry_policy_verdict keep/drop")
    print("case 42 OK")
