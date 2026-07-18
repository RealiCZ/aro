"""T52: RAYON_NUM_THREADS=1 pin for instruction-count measurement only.

Hermetic: fake SpecTarget + captured subprocess env at the icount/bench seams.
No cargo, no valgrind, no network.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock


_CLEAN_TOML = (
    '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n\n'
    '[profile.release]\nopt-level = 3\n'
)


def _minimal_spec_dict(repo: str) -> dict:
    return {
        "name": "rayon-pin-demo",
        "target_repo": {"path": repo, "baseline_ref": "HEAD"},
        "metric": "ns_per_call",
        "hot_path": {"file": "src/lib.rs", "fn": "f"},
        "benchmark_probe": {
            "probe": "probes/salt_msm.rs",
            "example": "e",
            "pkg": "ours",
        },
        "correctness_oracle": {
            "build": ["true"],
            "test": ["true"],
        },
        "constraints": {"editable": ["src/lib.rs"]},
    }


def case_68():
    """T52: icount env pins RAYON_NUM_THREADS=1; wall-clock does not; docs present."""
    print("=== case 68: icount RAYON_NUM_THREADS pin (T52) ===")
    from aro import spec as specmod
    from aro import target as targetmod

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        repo = td / "repo"
        work = td / "work"
        repo.mkdir()
        work.mkdir()
        (repo / "Cargo.toml").write_text(_CLEAN_TOML)
        (work / "Cargo.toml").write_text(_CLEAN_TOML)
        # Fake probe binary so icount reaches the valgrind subprocess seam.
        fake_bin = work / "probe-bin"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)

        sp = specmod.from_dict(_minimal_spec_dict(str(repo)))
        tgt = targetmod.SpecTarget(sp)

        # ------------------------------------------------------------------ env_for seam
        # Default / wallclock: no forced pin (parent env without the key → absent).
        clean_parent = {k: v for k, v in os.environ.items()
                        if k != "RAYON_NUM_THREADS"}
        with mock.patch.dict(os.environ, clean_parent, clear=True):
            wall_env = tgt.env_for(work, measurement_kind="wallclock")
            assert "RAYON_NUM_THREADS" not in wall_env, (
                f"wall-clock must not inject RAYON_NUM_THREADS; got "
                f"{wall_env.get('RAYON_NUM_THREADS')!r}")
            default_env = tgt.env_for(work)
            assert "RAYON_NUM_THREADS" not in default_env, (
                "env_for default must be wallclock (no rayon pin)")

            ic_env = tgt.env_for(work, measurement_kind="icount")
            assert ic_env.get("RAYON_NUM_THREADS") == "1", ic_env.get(
                "RAYON_NUM_THREADS")
        print("#68a OK: env_for icount pins 1; wallclock does not inject")

        # Pre-existing RAYON_NUM_THREADS=8: icount still forces 1; wallclock keeps 8.
        with mock.patch.dict(os.environ, {"RAYON_NUM_THREADS": "8"}, clear=False):
            ic_over = tgt.env_for(work, measurement_kind="icount")
            assert ic_over.get("RAYON_NUM_THREADS") == "1", (
                f"icount must override parent RAYON_NUM_THREADS; got "
                f"{ic_over.get('RAYON_NUM_THREADS')!r}")
            wall_keep = tgt.env_for(work, measurement_kind="wallclock")
            assert wall_keep.get("RAYON_NUM_THREADS") == "8", (
                f"wall-clock must inherit parent RAYON_NUM_THREADS=8; got "
                f"{wall_keep.get('RAYON_NUM_THREADS')!r}")
        print("#68b OK: parent RAYON_NUM_THREADS=8 overridden only for icount")

        # ------------------------------------------------------------------ icount subprocess seam
        # Capture the env actually passed to valgrind subprocess.run.
        captured = {}
        callgrind_text = (
            "events: Ir\n"
            "summary: 1000\n"
            "totals: 1000\n"
        )

        def fake_run(cmd, *args, **kwargs):
            # Only record the valgrind measurement invocation (not rustc -V).
            if cmd and cmd[0] == "valgrind":
                captured["env"] = dict(kwargs.get("env") or {})
                captured["cmd"] = list(cmd)
                # Write a minimal callgrind out so parse succeeds.
                out_flag = next(
                    (a for a in cmd if a.startswith("--callgrind-out-file=")),
                    None)
                if out_flag:
                    out_path = Path(out_flag.split("=", 1)[1])
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(callgrind_text)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"RAYON_NUM_THREADS": "8"}, clear=False), \
             mock.patch.object(tgt, "build_example", return_value=fake_bin), \
             mock.patch.object(tgt, "_rustc_version", return_value="rustc 1.80.0"), \
             mock.patch("subprocess.run", side_effect=fake_run):
            result = tgt.icount(work, scale=1)
            assert result.ir == 1000, result.ir
            assert captured.get("env"), "icount never invoked valgrind subprocess"
            assert captured["env"].get("RAYON_NUM_THREADS") == "1", (
                f"icount valgrind env must force RAYON_NUM_THREADS=1; got "
                f"{captured['env'].get('RAYON_NUM_THREADS')!r}")
            assert captured["env"].get("ARO_BENCH_SCALE") == "1"
        print("#68c OK: icount valgrind subprocess env forces RAYON_NUM_THREADS=1")

        # ------------------------------------------------------------------ wall-clock _cargo_run seam
        cargo_captured = {}

        def fake_cargo_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "cargo":
                cargo_captured["env"] = dict(kwargs.get("env") or {})
                # BENCH line so bench() can parse samples.
                return mock.Mock(
                    returncode=0,
                    stdout="BENCH 1.0 1.0 1.0\n",
                    stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"RAYON_NUM_THREADS": "8"}, clear=False), \
             mock.patch.object(tgt, "write_probe", return_value=None), \
             mock.patch("subprocess.run", side_effect=fake_cargo_run):
            m = tgt.bench(work, scale=1)
            assert m is not None
            assert cargo_captured.get("env"), "bench never invoked cargo subprocess"
            assert cargo_captured["env"].get("RAYON_NUM_THREADS") == "8", (
                f"wall-clock bench must NOT pin rayon; expected inherited 8, got "
                f"{cargo_captured['env'].get('RAYON_NUM_THREADS')!r}")
        print("#68d OK: wall-clock bench subprocess keeps parent RAYON_NUM_THREADS")

    # ------------------------------------------------------------------ docs greps
    root = Path(__file__).resolve().parents[1]
    ops = (root / "docs" / "OPERATIONS.md").read_text()
    assert "RAYON_NUM_THREADS=1" in ops, "OPERATIONS.md missing RAYON_NUM_THREADS=1"
    assert "salt-ipa" in ops and "0.00004945%" in ops, (
        "OPERATIONS.md missing salt-ipa case citation")
    assert "measurement_kind" in ops, "OPERATIONS.md should name measurement_kind seam"

    onboard = (root / "docs" / "ONBOARDING.md").read_text()
    assert "RAYON_NUM_THREADS=1" in onboard, (
        "ONBOARDING.md missing quick-reference RAYON_NUM_THREADS=1 row")
    assert "wall-clock stays parallel" in onboard or "wall-clock" in onboard.lower(), (
        "ONBOARDING.md row should note wall-clock stays parallel")
    print("#68e OK: OPERATIONS + ONBOARDING docs present")
    print("case_68 OK: icount RAYON_NUM_THREADS pin")
