"""Hermetic tests for `aro init` (T27): fixture cargo repos, no subprocess."""
from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path


def case_43():
    """T27: aro init — scaffold minimal spec + probes; workspace/force/hermetic."""
    print("=== case 43: aro init (onboarding scaffolder) ===")
    import subprocess as _sp

    from aro import cli as _cli
    from aro import init as _init
    from aro.spec import from_dict

    # Guard: inspection + scaffold must never spawn.
    _real_run = _sp.run

    def _no_subprocess(*a, **k):
        raise AssertionError(
            "case_43 hermeticity broken: subprocess.run fired under aro init")

    _sp.run = _no_subprocess
    try:
        # --- CLI wiring -------------------------------------------------------
        p = _cli.build_parser()
        args = p.parse_args([
            "init", "--repo", "/tmp/x", "--name", "n", "--package", "pkg", "--force",
        ])
        assert args.cmd == "init"
        assert args.repo == "/tmp/x"
        assert args.name == "n"
        assert args.package == "pkg"
        assert args.force is True
        print("#43a OK: CLI argparse wires init + flags")

        # --- single-package fixture → loadable spec + probe contracts ---------
        with tempfile.TemporaryDirectory(prefix="aro-init-single-") as td:
            repo = Path(td) / "crate"
            repo.mkdir()
            (repo / "Cargo.toml").write_text(
                '[package]\nname = "demo_crate"\nversion = "0.1.0"\nedition = "2021"\n',
                encoding="utf-8",
            )
            (repo / "src").mkdir()
            (repo / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")

            out_root = Path(td) / "aro-out"
            out_root.mkdir()
            buf = io.StringIO()
            result = _init.run_init(
                repo, name="demo", out_root=out_root, stdout=buf)
            printed = buf.getvalue()

            spec_path = result["spec"]
            assert spec_path.is_file(), spec_path
            raw = json.loads(spec_path.read_text(encoding="utf-8"))
            sp = from_dict(raw)
            assert sp.name == "demo"
            assert sp.repo == repo.resolve()
            assert sp.goal.metric == "ns_per_call"
            assert sp.goal.direction == "minimize"
            assert sp.bench["pkg"] == "demo_crate"
            assert sp.bench["sample_prefix"] == "BENCH"
            assert sp.bench["probe"] == "probes/demo-probe.rs"
            assert sp.differential["probe"] == "probes/demo-diff.rs"
            assert sp.differential["prefix"] == "DIFF"
            assert sp.build == [
                "cargo", "build", "--release", "-p", "demo_crate"]
            assert sp.test == [
                "cargo", "test", "--release", "-p", "demo_crate", "--lib"]
            assert sp.regions == ["src"]
            assert sp.constraints.get("no_new_deps") is True
            assert sp.constraints.get("byte_identical") is True
            assert sp.icount_epsilon_pct == 0.1
            # Certification-tier fields deliberately absent.
            for k in ("terminal_bench_targets", "measure_bin", "pinned_tools",
                      "control_lanes", "protected_row_families"):
                assert k not in raw, k

            bench_txt = result["bench_probe"].read_text(encoding="utf-8")
            diff_txt = result["diff_probe"].read_text(encoding="utf-8")
            assert "BENCH" in bench_txt
            assert "TODO(aro-init):" in bench_txt
            assert "DIFF" in diff_txt
            assert "TODO(aro-init):" in diff_txt
            assert "ARO_BENCH_SCALE" in bench_txt

            assert "Next steps" in printed
            assert "TODO" in printed or "probe" in printed.lower()
            assert "selfcheck" in printed
            assert "terminal_bench_targets" in printed or "Certification" in printed
            print("#43b OK: single-package init → from_dict + BENCH/DIFF + checklist")

            # --- refuse overwrite without --force; --force rewrites ------------
            buf2 = io.StringIO()
            try:
                _init.run_init(repo, name="demo", out_root=out_root, stdout=buf2)
                raise AssertionError("expected SystemExit refusing overwrite")
            except SystemExit as e:
                msg = str(e)
                assert "refusing to overwrite" in msg or "force" in msg.lower(), msg

            # Touch content so we can see overwrite.
            result["bench_probe"].write_text("// stale\n", encoding="utf-8")
            buf3 = io.StringIO()
            _init.run_init(
                repo, name="demo", force=True, out_root=out_root, stdout=buf3)
            assert "BENCH" in result["bench_probe"].read_text(encoding="utf-8")
            assert "// stale" not in result["bench_probe"].read_text(encoding="utf-8")
            print("#43c OK: refuse without --force; overwrite with --force")

        # --- workspace: exit 2 without --package; correct pkg with it ---------
        with tempfile.TemporaryDirectory(prefix="aro-init-ws-") as td:
            repo = Path(td) / "ws"
            (repo / "crates" / "alpha").mkdir(parents=True)
            (repo / "crates" / "beta").mkdir(parents=True)
            (repo / "Cargo.toml").write_text(
                '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n',
                encoding="utf-8",
            )
            for mem, name in (("alpha", "alpha"), ("beta", "beta")):
                d = repo / "crates" / mem
                (d / "Cargo.toml").write_text(
                    f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n',
                    encoding="utf-8",
                )
                (d / "src").mkdir()
                (d / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")

            try:
                _init.resolve_package(repo, None)
                raise AssertionError("expected exit 2 for multi-member workspace")
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                assert code == 2, e

            # Capture the list printed to stderr path via resolve — re-run
            # through run_init to see member listing.
            err = io.StringIO()
            import sys as _sys
            old_err = _sys.stderr
            _sys.stderr = err
            try:
                try:
                    _init.run_init(repo, out_root=Path(td) / "out")
                    raise AssertionError("expected SystemExit(2)")
                except SystemExit as e:
                    assert (e.code if isinstance(e.code, int) else 1) == 2
            finally:
                _sys.stderr = old_err
            err_txt = err.getvalue()
            assert "alpha" in err_txt and "beta" in err_txt, err_txt

            out_root = Path(td) / "aro-out"
            out_root.mkdir()
            buf = io.StringIO()
            result = _init.run_init(
                repo, package="beta", name="ws-beta",
                out_root=out_root, stdout=buf)
            raw = json.loads(result["spec"].read_text(encoding="utf-8"))
            assert raw["benchmark_probe"]["pkg"] == "beta"
            assert raw["correctness_oracle"]["build"] == [
                "cargo", "build", "--release", "-p", "beta"]
            assert raw["correctness_oracle"]["test"] == [
                "cargo", "test", "--release", "-p", "beta", "--lib"]
            assert raw["constraints"]["editable"] == ["crates/beta/src"]
            print("#43d OK: workspace lists members (exit 2); --package selects")

        # --- hand-parser: multi-line members + root package name --------------
        with tempfile.TemporaryDirectory(prefix="aro-init-parse-") as td:
            repo = Path(td)
            (repo / "Cargo.toml").write_text(
                '[package]\n'
                'name = "rooty"\n'
                'version = "0.1.0"\n'
                '\n'
                '[workspace]\n'
                'members = [\n'
                '  "crates/one",\n'
                '  "crates/two",\n'
                ']\n',
                encoding="utf-8",
            )
            info = _init.inspect_cargo_toml(repo)
            assert info["package_name"] == "rooty"
            assert info["members"] == ["crates/one", "crates/two"]
            assert info["is_workspace"] is True
            print("#43e OK: hand-parse package name + multi-line members")

        # --- default name from package (underscore → hyphen) ------------------
        assert _init.default_name("demo_crate") == "demo-crate"
        assert _init.probe_basenames("x") == ("x-probe", "x-diff")
        print("#43f OK: default_name + probe basenames")

    finally:
        _sp.run = _real_run

    print("case 43 OK")
