"""T47: profile_fidelity mode — measurement == adjudication build, not CGU != 1.

Hermetic: fixture Cargo.toml text + fake specs only. No cargo, no valgrind, no network.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock


# Exact historical codspeed-ci messages (regression-pin; must stay byte-identical).
_MSG_BENCH_CGU = (
    "profile-fidelity: [profile.bench] overrides codegen-units "
    "(=1) — measurement-only knobs are forbidden"
)
_MSG_BENCH_LTO = (
    "profile-fidelity: [profile.bench] overrides lto "
    "(=true) — measurement-only knobs are forbidden"
)
_MSG_REL_CGU1 = (
    "profile-fidelity: [profile.release] codegen-units = 1 "
    "distorts instruction counts vs production multi-CGU builds"
)

_CLEAN = (
    '[package]\nname = "x"\n\n'
    '[profile.release]\nopt-level = 3\nlto = "thin"\n'
)

# salt-shaped production release (CGU=1 + thin lto + panic=abort).
_SALT = (
    '[package]\nname = "salt"\n\n'
    '[profile.release]\n'
    'opt-level = 3\n'
    'lto = "thin"\n'
    'codegen-units = 1\n'
    'panic = "abort"\n'
)


def _minimal_spec_dict(repo: str, **extra) -> dict:
    d = {
        "name": "pf-demo",
        "target_repo": {"path": repo, "baseline_ref": "HEAD"},
        "metric": "ns",
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
    d.update(extra)
    return d


def case_63():
    """T47: profile_fidelity modes, fingerprint compare, spec load, call-site."""
    print("=== case 63: profile_fidelity mode (T47) ===")
    from aro import icount as ic
    from aro import spec as specmod
    from aro import target as targetmod

    # ------------------------------------------------------------------ codspeed-ci
    # Default (field absent / mode default): byte-identical to today's checks.
    assert ic.check_profile_fidelity(_CLEAN) is None
    assert ic.check_profile_fidelity(_CLEAN, mode="codspeed-ci") is None

    err = ic.check_profile_fidelity(
        _CLEAN + "\n[profile.bench]\ncodegen-units = 1\n")
    assert err == _MSG_BENCH_CGU, err

    err = ic.check_profile_fidelity(
        _CLEAN + "\n[profile.bench]\nlto = true\n")
    assert err == _MSG_BENCH_LTO, err

    rel_cgu1 = '[package]\nname = "x"\n\n[profile.release]\ncodegen-units = 1\n'
    err = ic.check_profile_fidelity(rel_cgu1)
    assert err == _MSG_REL_CGU1, err
    # salt-shaped profile is rejected under codspeed-ci (false positive for salt).
    err = ic.check_profile_fidelity(_SALT)
    assert err == _MSG_REL_CGU1, err
    # [profile.maxperf] is NOT a measurement profile — must NOT reject
    assert ic.check_profile_fidelity(
        _CLEAN + "\n[profile.maxperf]\ncodegen-units = 1\nlto = true\n") is None
    print("#63a OK: codspeed-ci default messages unchanged")

    # ------------------------------------------------------------------ repo-release
    # salt-shaped profile passes when candidate == baseline.
    assert ic.check_profile_fidelity(
        _SALT, mode="repo-release", baseline_cargo_toml_text=_SALT) is None

    # value change: codegen-units 1 → 16
    drifted = _SALT.replace("codegen-units = 1", "codegen-units = 16")
    err = ic.check_profile_fidelity(
        drifted, mode="repo-release", baseline_cargo_toml_text=_SALT)
    assert err is not None and "codegen-units" in err and "profile.release" in err, err
    assert "1" in err and "16" in err, err

    # key removed: lto
    no_lto = (
        '[package]\nname = "salt"\n\n'
        '[profile.release]\n'
        'opt-level = 3\n'
        'codegen-units = 1\n'
        'panic = "abort"\n'
    )
    err = ic.check_profile_fidelity(
        no_lto, mode="repo-release", baseline_cargo_toml_text=_SALT)
    assert err is not None and "lto" in err and "removed" in err, err
    assert "profile.release" in err, err

    # section added: [profile.bench] only in candidate
    with_bench = _SALT + "\n[profile.bench]\ninherits = \"release\"\n"
    err = ic.check_profile_fidelity(
        with_bench, mode="repo-release", baseline_cargo_toml_text=_SALT)
    assert err is not None and "profile.bench" in err and "added" in err, err

    # identical non-release profiles pass (maxperf present on both sides)
    both_max = _SALT + "\n[profile.maxperf]\ncodegen-units = 1\nlto = true\n"
    assert ic.check_profile_fidelity(
        both_max, mode="repo-release",
        baseline_cargo_toml_text=both_max) is None

    # missing baseline text is a hard error in repo-release
    err = ic.check_profile_fidelity(_SALT, mode="repo-release")
    assert err is not None and "baseline" in err.lower(), err
    print("#63b OK: repo-release comparative fingerprint")

    # ------------------------------------------------------------------ spec load
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        (repo / "Cargo.toml").write_text(_SALT)
        # absent → codspeed-ci
        sp = specmod.from_dict(_minimal_spec_dict(str(repo)))
        assert sp.profile_fidelity == "codspeed-ci"
        # explicit values parse
        sp2 = specmod.from_dict(_minimal_spec_dict(
            str(repo), profile_fidelity="repo-release"))
        assert sp2.profile_fidelity == "repo-release"
        sp3 = specmod.from_dict(_minimal_spec_dict(
            str(repo), profile_fidelity="codspeed-ci"))
        assert sp3.profile_fidelity == "codspeed-ci"
        # invalid → SystemExit
        try:
            specmod.from_dict(_minimal_spec_dict(
                str(repo), profile_fidelity="not-a-mode"))
            raise AssertionError("invalid profile_fidelity must SystemExit")
        except SystemExit as se:
            assert "profile_fidelity" in str(se), se
    print("#63c OK: spec load absent/explicit/invalid")

    # ------------------------------------------------------------------ call-site
    # SpecTarget.icount threads mode + baseline Cargo.toml into the guard.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        repo = td / "repo"
        work = td / "work"
        repo.mkdir()
        work.mkdir()
        (repo / "Cargo.toml").write_text(_SALT)
        (work / "Cargo.toml").write_text(_SALT)

        # Default mode: salt CGU=1 rejected before any build/valgrind.
        sp_ci = specmod.from_dict(_minimal_spec_dict(str(repo)))
        assert sp_ci.profile_fidelity == "codspeed-ci"
        tgt_ci = targetmod.SpecTarget(sp_ci)
        try:
            tgt_ci.icount(work)
            raise AssertionError("codspeed-ci must reject salt CGU=1 at icount")
        except RuntimeError as e:
            assert str(e) == _MSG_REL_CGU1, e

        # repo-release: matching salt profile passes the guard; short-circuit
        # after the check so we never need cargo/valgrind.
        sp_rr = specmod.from_dict(_minimal_spec_dict(
            str(repo), profile_fidelity="repo-release"))
        tgt_rr = targetmod.SpecTarget(sp_rr)
        with mock.patch.object(
                tgt_rr, "build_example",
                side_effect=RuntimeError("stop-after-fidelity")):
            try:
                tgt_rr.icount(work)
                raise AssertionError("expected stop-after-fidelity")
            except RuntimeError as e:
                assert "stop-after-fidelity" in str(e), e
                assert "profile-fidelity" not in str(e)

        # Drifted candidate under repo-release is rejected naming the key.
        (work / "Cargo.toml").write_text(
            _SALT.replace("codegen-units = 1", "codegen-units = 16"))
        try:
            tgt_rr.icount(work)
            raise AssertionError("repo-release must reject profile drift")
        except RuntimeError as e:
            msg = str(e)
            assert "codegen-units" in msg and "profile.release" in msg, msg
    print("#63d OK: SpecTarget.icount honors spec mode")
    print("case_63 OK: profile_fidelity mode")
