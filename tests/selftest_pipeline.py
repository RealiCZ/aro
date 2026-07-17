"""T44: aro pipeline — checkpointed campaign → PR orchestrator.

Hermetic: fake stage functions only. Never spawns cargo/git/sweep/certify/ship.
"""
from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path


def case_61():
    """T44: pipeline orchestrator — stage chain, checkpoints, resume, stops."""
    print("=== case 61: aro pipeline (checkpointed stage chain) ===")
    from aro import pipeline as pl
    from aro.cli import build_parser

    class _Spec:
        name = "pipeline-demo"

        def __getattr__(self, _k):
            return None

    spec = _Spec()
    SPEC_PATH = "targets/pipeline-demo.json"

    def _calls():
        return {
            "sweep": 0, "certify": 0, "gate": 0, "package": 0,
            "conformance": 0, "open": 0,
        }

    def _fakes(calls, *,
               certify_code=0,
               gate_code=0,
               package_code=0,
               package_files=None,
               conformance_code=0,
               open_code=0,
               open_url="https://github.com/example/repo/pull/99",
               workdir_name="ship-wd",
               branch_name="aro/ship-demo"):
        files = list(package_files if package_files is not None
                     else ["src/lib.rs", "src/hot.rs"])

        def sweep_fn(sp, out, **kw):
            calls["sweep"] += 1
            return 0

        def certify_fn(sp, out, **kw):
            calls["certify"] += 1
            return certify_code

        def gate_fn(sp, man, **kw):
            calls["gate"] += 1
            return gate_code

        def package_fn(sp, man, **kw):
            calls["package"] += 1
            wd = kw.get("workdir") or str(Path(man) / workdir_name)
            return {
                "exit_code": package_code,
                "workdir": wd,
                "branch": branch_name,
                "files_changed": files,
            }

        def conformance_fn(sp, workdir, **kw):
            calls["conformance"] += 1
            return conformance_code

        def open_fn(sp, man, workdir, **kw):
            calls["open"] += 1
            return {"exit_code": open_code, "url": open_url}

        return dict(
            sweep_fn=sweep_fn, certify_fn=certify_fn, gate_fn=gate_fn,
            package_fn=package_fn, conformance_fn=conformance_fn,
            open_fn=open_fn,
        )

    def _run(out_dir, calls, buf=None, **pl_kw):
        fakes = _fakes(calls, **{
            k: v for k, v in pl_kw.items()
            if k in (
                "certify_code", "gate_code", "package_code", "package_files",
                "conformance_code", "open_code", "open_url",
                "workdir_name", "branch_name",
            )
        })
        # strip code overrides from pipeline kwargs
        for k in list(pl_kw):
            if k.endswith("_code") or k in (
                    "package_files", "open_url", "workdir_name", "branch_name"):
                pl_kw.pop(k)
        sink = buf if buf is not None else io.StringIO()
        code = pl.pipeline(
            spec, out_dir,
            spec_path=SPEC_PATH,
            file=sink,
            **fakes,
            **pl_kw,
        )
        return code, sink.getvalue()

    def _state(out_dir):
        return json.loads((Path(out_dir) / pl.STATE_NAME).read_text())

    # ---------------------------------------------------------------- a -------
    # Fresh happy run: sweep→certify→gate→package once → exit 2 work order.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        calls = _calls()
        code, text = _run(run, calls)
        assert code == 2, f"a: expected exit 2, got {code}\n{text}"
        assert calls == {
            "sweep": 1, "certify": 1, "gate": 1, "package": 1,
            "conformance": 0, "open": 0,
        }, f"a: call counts {calls}"
        assert "src/lib.rs" in text and "src/hot.rs" in text, text
        assert "--continue" in text, text
        assert SPEC_PATH in text, text
        assert "dual-green" in text and "whitelist" in text, text
        st = _state(run)
        assert st["stages"]["sweep"] == "done"
        assert st["stages"]["certify"] == "done"
        assert st["stages"]["gate"] == "done"
        pkg = st["stages"]["package"]
        assert pkg["done"] is True
        assert pkg["branch"] == "aro/ship-demo"
        assert "ship-wd" in pkg["workdir"]
        assert "conformance" not in st["stages"] or not pl.is_stage_done(
            st["stages"], "conformance")
        print("  a. fresh happy → package stop exit 2 + work order OK")

    # ---------------------------------------------------------------- b -------
    # Resume: certify/gate/package NOT re-called; conformance+open; exit 0.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        calls = _calls()
        code1, _ = _run(run, calls)
        assert code1 == 2
        # reset counters for resume leg but keep state
        for k in calls:
            calls[k] = 0
        code2, text2 = _run(run, calls, continue_=True)
        assert code2 == 0, f"b: expected 0, got {code2}\n{text2}"
        assert calls["sweep"] == 0
        assert calls["certify"] == 0
        assert calls["gate"] == 0
        assert calls["package"] == 0
        assert calls["conformance"] == 1
        assert calls["open"] == 1
        assert "https://github.com/example/repo/pull/99" in text2, text2
        st = _state(run)
        assert st["stages"]["conformance"] == "done"
        assert st["stages"]["open"]["done"] is True
        assert st["stages"]["open"]["url"].endswith("/pull/99")
        print("  b. resume → conformance+open exit 0 with URL OK")

    # ---------------------------------------------------------------- c -------
    # conformance fail → exit 2; second resume re-runs conformance only.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        calls = _calls()
        code1, _ = _run(run, calls)
        assert code1 == 2
        for k in calls:
            calls[k] = 0
        code2, text2 = _run(run, calls, continue_=True, conformance_code=1)
        assert code2 == 2, f"c: expected 2, got {code2}\n{text2}"
        assert calls["conformance"] == 1
        assert calls["open"] == 0
        assert "conformance" in text2.lower(), text2
        st = _state(run)
        assert not pl.is_stage_done(st["stages"], "conformance")
        # second resume: only conformance (+ then open if we flip to pass)
        for k in calls:
            calls[k] = 0
        code3, text3 = _run(run, calls, continue_=True, conformance_code=0)
        assert code3 == 0, f"c3: {code3}\n{text3}"
        assert calls["conformance"] == 1
        assert calls["open"] == 1
        assert calls["package"] == 0
        print("  c. conformance fail → re-run conformance only OK")

    # ---------------------------------------------------------------- d -------
    # certify exit-2 propagates; state lacks certify; re-run re-invokes.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        calls = _calls()
        code1, text1 = _run(run, calls, certify_code=2)
        assert code1 == 2, f"d: {code1}\n{text1}"
        assert calls["certify"] == 1
        assert calls["gate"] == 0
        st = _state(run)
        assert st["stages"].get("sweep") == "done"
        assert not pl.is_stage_done(st["stages"], "certify")
        for k in calls:
            calls[k] = 0
        code2, _ = _run(run, calls, certify_code=0)
        assert code2 == 2  # package stop
        assert calls["certify"] == 1
        assert calls["gate"] == 1
        assert calls["package"] == 1
        print("  d. certify exit-2 not marked; re-run re-invokes OK")

    # ---------------------------------------------------------------- e -------
    # gate fail propagates prescription; not marked done.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        calls = _calls()
        code, text = _run(run, calls, gate_code=1)
        assert code == 2, f"e: {code}\n{text}"
        assert "re-certification" in text.lower() or "re-cert" in text.lower(), text
        assert calls["package"] == 0
        st = _state(run)
        assert st["stages"].get("certify") == "done"
        assert not pl.is_stage_done(st["stages"], "gate")
        for k in calls:
            calls[k] = 0
        code2, _ = _run(run, calls, gate_code=0)
        assert code2 == 2
        assert calls["gate"] == 1
        assert calls["package"] == 1
        assert calls["certify"] == 0
        print("  e. gate fail prescription; re-run gate only OK")

    # ---------------------------------------------------------------- f -------
    # --no-sweep marks skipped; --fresh clears state without other artifacts.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        artifact = run / "manifest.json"
        artifact.write_text('{"accepted":[]}\n')
        calls = _calls()
        code, text = _run(run, calls, no_sweep=True)
        assert code == 2, f"f: {code}\n{text}"
        assert calls["sweep"] == 0
        st = _state(run)
        assert st["stages"]["sweep"] == "skipped"
        # --fresh: state cleared, fns re-invoked; artifact survives
        for k in calls:
            calls[k] = 0
        code2, text2 = _run(run, calls, fresh=True, no_sweep=True)
        assert code2 == 2
        assert calls["certify"] == 1  # re-invoked after fresh
        assert calls["package"] == 1
        assert artifact.is_file(), "fresh must not delete campaign artifacts"
        assert "--fresh cleared" in text2 or "fresh" in text2.lower()
        st2 = _state(run)
        assert st2["stages"]["sweep"] == "skipped"
        print("  f. --no-sweep + --fresh OK")

    # ---------------------------------------------------------------- g -------
    # open refusal → exit 2, not marked; re-run re-tries open only.
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        calls = _calls()
        code1, _ = _run(run, calls)
        assert code1 == 2
        for k in calls:
            calls[k] = 0
        code2, text2 = _run(run, calls, continue_=True, open_code=1)
        assert code2 == 2, f"g: {code2}\n{text2}"
        assert calls["conformance"] == 1
        assert calls["open"] == 1
        st = _state(run)
        assert st["stages"]["conformance"] == "done"
        assert not pl.is_stage_done(st["stages"], "open")
        for k in calls:
            calls[k] = 0
        code3, text3 = _run(run, calls, continue_=True, open_code=0)
        assert code3 == 0, f"g3: {code3}\n{text3}"
        assert calls["open"] == 1
        assert calls["conformance"] == 0
        assert calls["package"] == 0
        print("  g. open refusal; re-try open only OK")

    # ---------------------------------------------------------------- h -------
    # Docs greps + CLI parser seam.
    root = Path(__file__).resolve().parents[1]
    ops = (root / "docs" / "OPERATIONS.md").read_text()
    assert "13.10" in ops or "pipeline" in ops.lower()
    assert "pipeline-state.json" in ops
    assert "aro pipeline" in ops
    rtp = (root / "skill" / "references" / "run-to-pr.md").read_text()
    assert "aro pipeline" in rtp
    cop = (root / "skill" / "references" / "campaign-operator.md").read_text()
    assert "aro pipeline" in cop

    p = build_parser()
    ns = p.parse_args([
        "pipeline", "targets/x.json", "--manifest", "/tmp/run",
        "--continue", "--no-sweep", "--fresh", "--workdir", "/tmp/wd",
    ])
    assert ns.cmd == "pipeline"
    assert ns.pipeline_continue is True
    assert ns.no_sweep is True
    assert ns.fresh is True
    assert ns.workdir == "/tmp/wd"
    print("  h. docs greps + CLI parser OK")

    print("case 61 OK")
