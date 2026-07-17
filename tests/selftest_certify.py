"""T43: aro certify — candidates → stamped manifest (decision table executable).

Hermetic: fake stage functions only. Never spawns cargo/git/measure/reverify.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path


def case_60():
    """T43: certify orchestrator — verdict dispatch + greedy prune + re-entry."""
    print("=== case 60: aro certify (orchestrator + prune policy) ===")
    from aro import certify as cz
    from aro import terminal as tm
    from aro.cli import build_parser

    # Minimal stand-in spec (stage fakes never call into it).
    class _Spec:
        name = "certify-demo"
        outlier_quarantine_pct = 5.0

        def __getattr__(self, _k):
            return None

    spec = _Spec()

    def _write_manifest(run: Path, orders=(1, 2, 3)):
        accepted = []
        for o in orders:
            accepted.append({
                "order": o, "id": f"c{o}", "fn": f"fn{o}",
                "attempt": f"a{o}", "files": ["src/lib.rs"],
                "delta_pct": -1.0, "regime": "byte-identical",
                "critic_verdict": "pass", "mergeable": True,
                "hypothesis": f"h{o}",
                "patch_path": f"a{o}/patches/c{o}.txt",
                "acceptance_seq": o - 1,
                "parent": "base" if o == 1 else f"c{o - 1}",
            })
            (run / f"a{o}" / "patches").mkdir(parents=True, exist_ok=True)
            (run / f"a{o}" / "patches" / f"c{o}.txt").write_text("x\n")
        man = {
            "spec": "certify-demo", "baseline_ref": "abc",
            "accepted": accepted, "files_touched": ["src/lib.rs"], "notes": "",
        }
        (run / "manifest.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=1) + "\n")
        return man

    def _term_doc(verdict, *, orders, rows=None, notes=None):
        return {
            "verdict": verdict,
            "bench_ir_rows": {
                (r.get("row_key") if isinstance(r, dict) else k): (
                    r.get("delta_pct") if isinstance(r, dict) else v)
                for k, v, r in []
            } or {},
            "rows": rows or [],
            "notes": notes or [],
            "profile_fingerprint": "fp-cert",
            "env_fingerprint": "e=1",
            "epsilon_pct": 0.1,
            "rounds": 3,
            "floors_source": "default",
            "baseline_sha": "a" * 40,
            "measured_orders": list(orders),
        }

    def _row(key, delta, floor, status="regressed"):
        # base/cand so verify would be consistent if ever called; fakes skip it.
        base = 10000
        cand = int(round(base * (1.0 + delta / 100.0)))
        return {
            "row_key": key,
            "base_ir": base,
            "cand_ir": cand,
            "delta_pct": float(delta),
            "floor_pct": float(floor),
            "status": status,
        }

    # ---------------------------------------------------------------- a -------
    # CONFIRMED first pass → stamp full survivors, exit 0, no prune file.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run)
        calls = {"recheck": 0, "terminal": 0, "ablate": 0, "stamp": 0}
        stamped_orders = []

        def recheck_fn(sp, out, orders):
            calls["recheck"] += 1
            surv = [1, 2, 3]
            doc = {
                "preflight": "pass",
                "entries": [
                    {"order": o, "id": f"c{o}", "fn": f"fn{o}",
                     "verdict": "reverify-pass"}
                    for o in surv
                ],
            }
            (Path(out) / "reverify.json").write_text(
                json.dumps(doc, indent=1) + "\n")
            return {
                "preflight": "pass", "survivors": surv, "doc": doc,
                "baseline_dir": None, "candidate_dir": None, "target": None,
            }

        def terminal_fn(sp, out, orders, round_n, **kw):
            calls["terminal"] += 1
            doc = _term_doc(tm.TERMINAL_CONFIRMED, orders=orders, rows=[
                _row("hot/a", -3.0, 1.0, "improved"),
                _row("hot/b", 0.1, 1.0, "untouched"),
            ])
            # Fix bench_ir_rows for stamp path.
            doc["bench_ir_rows"] = {"hot/a": -3.0, "hot/b": 0.1}
            path = cz.terminal_artifact_path(out, round_n)
            path.write_text(json.dumps(doc, indent=1) + "\n")
            return {"verdict": doc["verdict"], "path": path, "doc": doc}

        def ablate_fn(sp, out, orders):
            calls["ablate"] += 1
            raise AssertionError("ablate must not run on CONFIRMED")

        def stamp_fn(sp, out, terminal_path, orders):
            calls["stamp"] += 1
            stamped_orders.append(list(orders))
            man = json.loads((Path(out) / "manifest.json").read_text())
            for a in man["accepted"]:
                if a["order"] in set(orders):
                    a["terminal"] = tm.TERMINAL_CONFIRMED
                    a["terminal_stamp"] = {
                        "verdict": tm.TERMINAL_CONFIRMED,
                        "source": str(terminal_path),
                        "sha256": "deadbeef",
                    }
                    a["mergeable"] = True
                else:
                    a["terminal"] = tm.TERMINAL_NOT_MEASURED
                    a["mergeable"] = False
            (Path(out) / "manifest.json").write_text(
                json.dumps(man, indent=1) + "\n")
            return man

        r = cz.certify(
            spec, run,
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=ablate_fn, stamp_fn=stamp_fn,
        )
        assert r.exit_code == 0, r
        assert r.stamped is True
        assert r.verdict == tm.TERMINAL_CONFIRMED
        assert stamped_orders == [[1, 2, 3]], stamped_orders
        assert calls == {"recheck": 1, "terminal": 1, "ablate": 0, "stamp": 1}
        assert not cz.prune_ledger_path(run).exists()
    print("  a OK: CONFIRMED first pass → stamp full set, no prune")

    # ---------------------------------------------------------------- b -------
    # MIXED → 1 prune (tradeable-cap violation attrs to order 2) → WITH_TRADE.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run)
        calls = {"terminal": 0, "ablate": 0, "stamp": 0}
        term_seq = [tm.TERMINAL_MIXED, tm.TERMINAL_CONFIRMED_WITH_TRADE]
        stamped_orders = []

        def recheck_fn(sp, out, orders):
            surv = [1, 2, 3]
            doc = {
                "preflight": "pass",
                "entries": [
                    {"order": o, "verdict": "reverify-pass",
                     "id": f"c{o}", "fn": f"fn{o}"}
                    for o in surv
                ],
            }
            (Path(out) / "reverify.json").write_text(json.dumps(doc) + "\n")
            return {"preflight": "pass", "survivors": surv, "doc": doc,
                    "baseline_dir": None, "candidate_dir": None}

        def terminal_fn(sp, out, orders, round_n, **kw):
            calls["terminal"] += 1
            v = term_seq[calls["terminal"] - 1]
            if v == tm.TERMINAL_MIXED:
                rows = [
                    _row("trade_fam/r1", -4.0, 1.0, "improved"),
                    _row("trade_fam/r2", 2.5, 1.0, "regressed"),  # over cap 1.0
                ]
                notes = []
            else:
                rows = [
                    _row("trade_fam/r1", -4.0, 1.0, "improved"),
                    _row("trade_fam/r2", 0.5, 1.0, "regressed"),  # within cap
                ]
                notes = ["traded: trade_fam/r2 +0.5000% (cap 1.0%)"]
            doc = _term_doc(v, orders=orders, rows=rows, notes=notes)
            doc["bench_ir_rows"] = {r["row_key"]: r["delta_pct"] for r in rows}
            path = cz.terminal_artifact_path(out, round_n)
            path.write_text(json.dumps(doc, indent=1) + "\n")
            return {"verdict": v, "path": path, "doc": doc}

        def ablate_fn(sp, out, orders):
            calls["ablate"] += 1
            # Order 2 is the max contributor on the violated tradeable row.
            return {
                "entries": [
                    {"order": 1, "id": "c1", "fn": "fn1",
                     "marginal_rows_summary": {
                         "trade_fam/r1": -2.0, "trade_fam/r2": 0.1}},
                    {"order": 2, "id": "c2", "fn": "fn2",
                     "marginal_rows_summary": {
                         "trade_fam/r1": -1.0, "trade_fam/r2": 2.2}},
                    {"order": 3, "id": "c3", "fn": "fn3",
                     "marginal_rows_summary": {
                         "trade_fam/r1": -1.0, "trade_fam/r2": 0.2}},
                ],
                "proposal": [{"order": 1}, {"order": 3}],
                "dropped": [{"order": 2}],
            }

        def stamp_fn(sp, out, terminal_path, orders):
            calls["stamp"] += 1
            stamped_orders.append(list(orders))
            return json.loads((Path(out) / "manifest.json").read_text())

        # Spec with tradeable policy so collect_violations flags cap breach.
        class _PolSpec(_Spec):
            protected_row_families = ["oracle"]
            tradeable_regression_cap_pct = 1.0
            protected_hysteresis = {"margin_pp": 0.05, "floor_multiple": 1.5}

        # classify_subject_regression reads from row + families/cap kwargs
        # via resolve_* when spec is used — monkey via real resolve needs
        # spec_field. Use a real TargetSpec-like through certify's resolve
        # helpers: inject by wrapping collect to use explicit policy? The
        # orchestrator calls collect_violations(terminal_doc, spec=spec)
        # which uses resolve_protected_row_families(spec). Provide attributes
        # that spec_field can see.
        from aro import spec as specmod
        pol = specmod.from_dict({
            "name": "certify-demo",
            "target_repo": {"path": str(run), "baseline_ref": "HEAD"},
            "metric": "ns",
            "hot_path": {"file": "src/lib.rs", "fn": "f"},
            "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "run": {"generator": "agentic",
                    "stop": {"max_rounds": 1, "dry_rounds": 1},
                    "aa_runs": 1, "ab_pairs": 1},
            "constraints": {"editable": ["src/lib.rs"]},
            "protected_row_families": ["oracle"],
            "tradeable_regression_cap_pct": 1.0,
            "protected_hysteresis": {"margin_pp": 0.05, "floor_multiple": 1.5},
        })

        r = cz.certify(
            pol, run,
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=ablate_fn, stamp_fn=stamp_fn,
        )
        assert r.exit_code == 0, (r.exit_code, r.message)
        assert r.stamped is True
        assert r.verdict == tm.TERMINAL_CONFIRMED_WITH_TRADE
        assert stamped_orders == [[1, 3]], stamped_orders
        assert calls["ablate"] == 1
        assert calls["terminal"] == 2
        assert calls["stamp"] == 1
        ledger = cz.prune_ledger_path(run)
        assert ledger.is_file()
        lines = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
        assert len(lines) == 1
        assert lines[0]["dropped_order"] == 2
        assert lines[0]["violated_row"] == "trade_fam/r2"
        assert lines[0]["round"] == 1
        assert "evidence" in lines[0] and "ablate.json" in lines[0]["evidence"]
        assert lines[0]["fn"] == "fn2"
    print("  b OK: MIXED → 1 prune → WITH_TRADE; ledger has drop + evidence")

    # ---------------------------------------------------------------- c -------
    # MIXED → 2 rounds still violating → exit 2, stamp NOT called.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run, orders=(1, 2, 3, 4))
        calls = {"stamp": 0, "ablate": 0, "terminal": 0}

        def recheck_fn(sp, out, orders):
            surv = [1, 2, 3, 4]
            doc = {"preflight": "pass", "entries": [
                {"order": o, "verdict": "reverify-pass", "id": f"c{o}",
                 "fn": f"fn{o}"} for o in surv]}
            (Path(out) / "reverify.json").write_text(json.dumps(doc) + "\n")
            return {"preflight": "pass", "survivors": surv, "doc": doc,
                    "baseline_dir": None, "candidate_dir": None}

        def terminal_fn(sp, out, orders, round_n, **kw):
            calls["terminal"] += 1
            # Always MIXED with a tradeable violation; row key stable so
            # ablate can keep naming the highest remaining order.
            rows = [
                _row("trade_fam/r1", -5.0, 1.0, "improved"),
                _row("trade_fam/r2", 3.0, 1.0, "regressed"),
            ]
            doc = _term_doc(tm.TERMINAL_MIXED, orders=orders, rows=rows)
            doc["bench_ir_rows"] = {r["row_key"]: r["delta_pct"] for r in rows}
            path = cz.terminal_artifact_path(out, round_n)
            path.write_text(json.dumps(doc, indent=1) + "\n")
            return {"verdict": tm.TERMINAL_MIXED, "path": path, "doc": doc}

        def ablate_fn(sp, out, orders):
            calls["ablate"] += 1
            # Max contribution on r2 among current orders = max order.
            entries = []
            for o in orders:
                entries.append({
                    "order": o, "id": f"c{o}", "fn": f"fn{o}",
                    "marginal_rows_summary": {
                        "trade_fam/r1": -1.0,
                        "trade_fam/r2": float(o),  # higher order → larger
                    },
                })
            return {"entries": entries}

        def stamp_fn(sp, out, terminal_path, orders):
            calls["stamp"] += 1
            raise AssertionError("stamp must not run when prune fails to converge")

        from aro import spec as specmod
        pol = specmod.from_dict({
            "name": "certify-demo",
            "target_repo": {"path": str(run), "baseline_ref": "HEAD"},
            "metric": "ns",
            "hot_path": {"file": "src/lib.rs", "fn": "f"},
            "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "run": {"generator": "agentic",
                    "stop": {"max_rounds": 1, "dry_rounds": 1},
                    "aa_runs": 1, "ab_pairs": 1},
            "constraints": {"editable": ["src/lib.rs"]},
            "protected_row_families": ["oracle"],
            "tradeable_regression_cap_pct": 1.0,
        })

        r = cz.certify(
            pol, run,
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=ablate_fn, stamp_fn=stamp_fn,
        )
        assert r.exit_code == 2, (r.exit_code, r.message)
        assert r.stamped is False
        assert calls["stamp"] == 0
        assert calls["ablate"] == 2  # two prune rounds
        assert calls["terminal"] == 3  # initial + 2 re-measures
        assert "still violating" in r.message or "2 prune" in r.message
        assert "surviving violations" in r.message
        assert "certify-prune.jsonl" in r.message
        ledger_lines = [
            json.loads(x) for x in cz.prune_ledger_path(run)
            .read_text().splitlines() if x.strip()
        ]
        assert len(ledger_lines) == 2
        assert {x["dropped_order"] for x in ledger_lines} == {4, 3}
    print("  c OK: MIXED ×2 still violating → exit 2, no stamp")

    # ---------------------------------------------------------------- d -------
    # Protected-family violation prunes the attributed entry (zero tolerance).
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run)
        stamped_orders = []

        def recheck_fn(sp, out, orders):
            surv = [1, 2, 3]
            doc = {"preflight": "pass", "entries": [
                {"order": o, "verdict": "reverify-pass", "id": f"c{o}",
                 "fn": f"fn{o}"} for o in surv]}
            (Path(out) / "reverify.json").write_text(json.dumps(doc) + "\n")
            return {"preflight": "pass", "survivors": surv, "doc": doc,
                    "baseline_dir": None, "candidate_dir": None}

        n_term = {"n": 0}

        def terminal_fn(sp, out, orders, round_n, **kw):
            n_term["n"] += 1
            if n_term["n"] == 1:
                rows = [
                    _row("hot/a", -3.0, 1.0, "improved"),
                    # protected family oracle, floor 1.0, H≈1.5 → 2.0 is violation
                    _row("oracle/sload", 2.0, 1.0, "regressed"),
                ]
                v = tm.TERMINAL_MIXED
            else:
                rows = [
                    _row("hot/a", -3.0, 1.0, "improved"),
                    _row("oracle/sload", 0.2, 1.0, "untouched"),
                ]
                v = tm.TERMINAL_CONFIRMED
            doc = _term_doc(v, orders=orders, rows=rows)
            doc["bench_ir_rows"] = {r["row_key"]: r["delta_pct"] for r in rows}
            path = cz.terminal_artifact_path(out, round_n)
            path.write_text(json.dumps(doc, indent=1) + "\n")
            return {"verdict": v, "path": path, "doc": doc}

        def ablate_fn(sp, out, orders):
            return {
                "entries": [
                    {"order": 1, "marginal_rows_summary": {
                        "hot/a": -3.0, "oracle/sload": 0.1}},
                    {"order": 2, "marginal_rows_summary": {
                        "hot/a": 0.0, "oracle/sload": 1.9}},  # max on protected
                    {"order": 3, "marginal_rows_summary": {
                        "hot/a": 0.0, "oracle/sload": 0.0}},
                ],
            }

        def stamp_fn(sp, out, terminal_path, orders):
            stamped_orders.append(list(orders))
            return json.loads((Path(out) / "manifest.json").read_text())

        from aro import spec as specmod
        pol = specmod.from_dict({
            "name": "certify-demo",
            "target_repo": {"path": str(run), "baseline_ref": "HEAD"},
            "metric": "ns",
            "hot_path": {"file": "src/lib.rs", "fn": "f"},
            "benchmark_probe": {"probe": "p.rs", "example": "e", "pkg": "ours"},
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "run": {"generator": "agentic",
                    "stop": {"max_rounds": 1, "dry_rounds": 1},
                    "aa_runs": 1, "ab_pairs": 1},
            "constraints": {"editable": ["src/lib.rs"]},
            "protected_row_families": ["oracle"],
            "tradeable_regression_cap_pct": 1.0,
            "protected_hysteresis": {"margin_pp": 0.05, "floor_multiple": 1.5},
        })

        r = cz.certify(
            pol, run,
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=ablate_fn, stamp_fn=stamp_fn,
        )
        assert r.exit_code == 0, r.message
        assert stamped_orders == [[1, 3]], stamped_orders
        line = json.loads(cz.prune_ledger_path(run).read_text().splitlines()[0])
        assert line["dropped_order"] == 2
        assert line["violated_row"] == "oracle/sload"
    print("  d OK: protected-family violation → drop attributed entry")

    # ---------------------------------------------------------------- e -------
    # CONTROL_ANOMALY → exit 2 immediately, ablate never called.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run)
        calls = {"ablate": 0, "stamp": 0}

        def recheck_fn(sp, out, orders):
            return {"preflight": "pass", "survivors": [1, 2], "doc": {},
                    "baseline_dir": None, "candidate_dir": None}

        def terminal_fn(sp, out, orders, round_n, **kw):
            doc = _term_doc(tm.TERMINAL_CONTROL_ANOMALY, orders=orders, rows=[
                _row("ctrl_lane", 4.0, 2.0, "control-anomaly"),
            ])
            path = cz.terminal_artifact_path(out, round_n)
            path.write_text(json.dumps(doc, indent=1) + "\n")
            return {"verdict": tm.TERMINAL_CONTROL_ANOMALY, "path": path,
                    "doc": doc}

        def ablate_fn(sp, out, orders):
            calls["ablate"] += 1
            raise AssertionError("ablate must not run on CONTROL_ANOMALY")

        def stamp_fn(sp, out, terminal_path, orders):
            calls["stamp"] += 1
            raise AssertionError("stamp must not run on CONTROL_ANOMALY")

        r = cz.certify(
            spec, run,
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=ablate_fn, stamp_fn=stamp_fn,
        )
        assert r.exit_code == 2
        assert r.verdict == tm.TERMINAL_CONTROL_ANOMALY
        assert calls["ablate"] == 0
        assert calls["stamp"] == 0
        assert "A/A" in r.message or "disambiguation" in r.message
        assert "pruning must NOT run" in r.message
    print("  e OK: CONTROL_ANOMALY → exit 2, no ablate")

    # ---------------------------------------------------------------- f -------
    # recheck preflight failure propagates; terminal never called.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run)
        calls = {"terminal": 0}

        def recheck_fn(sp, out, orders):
            return {
                "preflight": "fail",
                "detail": "pre-flight failed: UNPATCHED baseline broken",
                "survivors": [], "doc": {"preflight": "fail"},
            }

        def terminal_fn(sp, out, orders, round_n, **kw):
            calls["terminal"] += 1
            raise AssertionError("terminal must not run after preflight fail")

        r = cz.certify(
            spec, run,
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=lambda *a, **k: None,
            stamp_fn=lambda *a, **k: None,
        )
        assert r.exit_code == 1
        assert "pre-flight failed" in r.message
        assert calls["terminal"] == 0
    print("  f OK: recheck preflight fail → exit 1, no terminal")

    # ---------------------------------------------------------------- g -------
    # --from terminal skips recheck_fn (artifact reuse).
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run)
        # Prior reverify artifact.
        rev = {
            "preflight": "pass",
            "entries": [
                {"order": 1, "verdict": "reverify-pass", "id": "c1", "fn": "fn1"},
                {"order": 2, "verdict": "reverify-pass", "id": "c2", "fn": "fn2"},
            ],
        }
        (run / "reverify.json").write_text(json.dumps(rev) + "\n")
        calls = {"recheck": 0, "terminal": 0, "stamp": 0}

        def recheck_fn(sp, out, orders):
            calls["recheck"] += 1
            raise AssertionError("recheck_fn must not run with --from terminal")

        def terminal_fn(sp, out, orders, round_n, **kw):
            calls["terminal"] += 1
            assert sorted(orders) == [1, 2], orders
            doc = _term_doc(tm.TERMINAL_CONFIRMED, orders=orders, rows=[
                _row("hot/a", -2.0, 1.0, "improved"),
            ])
            doc["bench_ir_rows"] = {"hot/a": -2.0}
            path = cz.terminal_artifact_path(out, round_n)
            path.write_text(json.dumps(doc, indent=1) + "\n")
            return {"verdict": tm.TERMINAL_CONFIRMED, "path": path, "doc": doc}

        def stamp_fn(sp, out, terminal_path, orders):
            calls["stamp"] += 1
            return json.loads((Path(out) / "manifest.json").read_text())

        r = cz.certify(
            spec, run, from_stage="terminal",
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=lambda *a, **k: None, stamp_fn=stamp_fn,
        )
        assert r.exit_code == 0, r.message
        assert calls["recheck"] == 0
        assert calls["terminal"] == 1
        assert calls["stamp"] == 1

        # Also: when terminal-c1.json already present, do not re-call terminal_fn.
        calls["terminal"] = 0
        calls["stamp"] = 0
        r2 = cz.certify(
            spec, run, from_stage="terminal",
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=lambda *a, **k: None, stamp_fn=stamp_fn,
        )
        assert r2.exit_code == 0
        assert calls["terminal"] == 0  # reused artifact
        assert calls["stamp"] == 1
    print("  g OK: --from terminal skips recheck; reuses terminal-cN artifact")

    # ---------------------------------------------------------------- h -------
    # REGRESSED → exit 2 with decision-table next action; CLI surfaces.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        _write_manifest(run)

        def recheck_fn(sp, out, orders):
            return {"preflight": "pass", "survivors": [1], "doc": {},
                    "baseline_dir": None, "candidate_dir": None}

        def terminal_fn(sp, out, orders, round_n, **kw):
            doc = _term_doc(tm.TERMINAL_REGRESSED, orders=orders, rows=[
                _row("hot/a", 3.0, 1.0, "regressed"),
            ])
            path = cz.terminal_artifact_path(out, round_n)
            path.write_text(json.dumps(doc, indent=1) + "\n")
            return {"verdict": tm.TERMINAL_REGRESSED, "path": path, "doc": doc}

        r = cz.certify(
            spec, run,
            recheck_fn=recheck_fn, terminal_fn=terminal_fn,
            ablate_fn=lambda *a, **k: None,
            stamp_fn=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("no stamp")),
        )
        assert r.exit_code == 2
        assert r.verdict == tm.TERMINAL_REGRESSED
        assert "no PR" in r.message

    p = build_parser()
    ns = p.parse_args([
        "certify", "targets/x.json", "--manifest", "/tmp/run",
        "--from", "prune", "--orders", "1,3",
    ])
    assert ns.cmd == "certify"
    assert ns.from_stage == "prune"
    assert ns.orders == "1,3"
    assert ns.manifest == "/tmp/run"
    print("  h OK: REGRESSED work order + CLI argparse")

    # ---------------------------------------------------------------- i -------
    # Docs greps: prune policy wording + ratification date in OPERATIONS.
    ops = Path("docs/OPERATIONS.md").read_text()
    assert "aro certify" in ops or "`aro certify`" in ops
    assert "greedy attribution-based pruning" in ops
    assert "2026-07-17" in ops
    assert "CONTROL_ANOMALY never pruned" in ops or (
        "CONTROL_ANOMALY" in ops and "never pruned" in ops)
    assert "≤2 rounds" in ops or "at most 2 rounds" in ops or "max 2 rounds" in ops
    rtp = Path("skill/references/run-to-pr.md").read_text()
    assert "aro certify" in rtp
    print("  i OK: docs greps (OPERATIONS prune policy + run-to-pr)")

    print("case_60 OK: certify orchestrator")
