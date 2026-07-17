"""T49: onboarding hardening — terminal_probe_scales, cost preflight, docs.

Hermetic: no cargo, no network, no measure_bin. Fake timers / runners only.
"""
from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


def case_65():
    """T49: terminal_probe_scales defaults, cost preflight, Class A/B docs."""
    from aro import terminal as tm
    from aro import spec as specmod

    print("=== case 65: onboarding hardening (T49) ===")

    def _base_spec_dict(*, name="onboard-t49", terminal_lane="probe",
                        terminal_probe_workloads=1,
                        terminal_probe_scales=None,
                        bench_scales=None, **extra):
        d = {
            "name": name,
            "target_repo": {"path": "/tmp/does-not-need-to-exist-t49",
                            "baseline_ref": "HEAD"},
            "metric": "ns_per_call",
            "hot_path": {"file": "src/lib.rs", "fn": "hot"},
            "benchmark_probe": {
                "pkg": "p", "example": "e",
                "probe": "fixtures/mini-target/probes/mini_target.rs",
            },
            "correctness_oracle": {"build": ["true"], "test": ["true"]},
            "constraints": {"editable": ["src"]},
            "run": {
                "aa_runs": 1, "ab_pairs": 1, "timeout": 60,
                "bench_scales": (bench_scales if bench_scales is not None
                                 else [1, 8, 64]),
            },
            "icount_epsilon_pct": 0.1,
            "terminal_default_floor_pct": 1.0,
            "terminal_measure_rounds": 1,
            "terminal_lane": terminal_lane,
            "terminal_probe_workloads": terminal_probe_workloads,
        }
        if terminal_probe_scales is not None:
            d["terminal_probe_scales"] = terminal_probe_scales
        d.update(extra)
        return d

    def _sp(**kw):
        return specmod.from_dict(_base_spec_dict(**kw))

    def _fake_factory(spec, baseline=None):
        return [
            tm.ProbeVariant(name="orig", params={"kind": "original"}),
            tm.ProbeVariant(name="vA", params={"kind": "synthetic", "seed": 1}),
        ]

    # --- (a) terminal_probe_scales: absent → [1,8]; NOT run.bench_scales ------
    sp_absent = _sp(bench_scales=[1, 8, 64])  # wall-clock ladder includes 64
    assert "terminal_probe_scales" not in (sp_absent.raw or {})
    assert sp_absent.terminal_probe_scales == (1, 8), sp_absent.terminal_probe_scales
    scales = tm.resolve_probe_scales(sp_absent)
    assert scales == [1, 8], scales
    assert 64 not in scales, "64 must not leak from run.bench_scales into probe matrix"
    # Row keys for the default matrix must not include scale 64
    variants = _fake_factory(sp_absent)
    keys = tm.probe_row_keys(variants, scales)
    assert all("/64" not in k for k in keys), keys
    assert "probe/orig/1" in keys and "probe/orig/8" in keys

    sp_explicit = _sp(terminal_probe_scales=[1, 4, 16], bench_scales=[1, 8, 64])
    assert sp_explicit.terminal_probe_scales == (1, 4, 16)
    assert tm.resolve_probe_scales(sp_explicit) == [1, 4, 16]
    assert 64 not in tm.resolve_probe_scales(sp_explicit)

    for bad in ([], [-1], [0], [1, -2], ["8"], [1.5], [True], None):
        # None is absent → default; skip as invalid
        if bad is None:
            continue
        try:
            _sp(terminal_probe_scales=bad)
            assert False, f"invalid terminal_probe_scales {bad!r} must SystemExit"
        except SystemExit as e:
            assert "terminal_probe_scales" in str(e), e
    print("#65a OK: terminal_probe_scales default [1,8]; ignores bench_scales; invalid→SystemExit")

    # --- (b) cost preflight math (fake base cost) ----------------------------
    # base=10s @ scale 1; scales [1,8]; 2 variants; 3 rounds; 1 side
    # cost(1)=10, cost(8)=80; one_pass = 2*(10+80)=180; total = 180*3*1 = 540
    est = tm.estimate_probe_matrix_secs(
        10.0, [1, 8], n_variants=2, n_rounds=3, n_sides=1)
    assert est["min_scale"] == 1
    assert est["per_scale_secs"][1] == 10.0
    assert est["per_scale_secs"][8] == 80.0
    assert est["one_pass_secs"] == 180.0
    assert est["total_secs"] == 540.0
    assert est["n_measurements"] == 2 * 2 * 3 * 1  # variants × scales × rounds × sides

    # n_sides=2 (measure path: baseline + candidate)
    est2 = tm.estimate_probe_matrix_secs(
        10.0, [1, 8], n_variants=2, n_rounds=3, n_sides=2)
    assert est2["total_secs"] == 1080.0

    # Linear in scale relative to min (min not always 1)
    est3 = tm.estimate_probe_matrix_secs(
        5.0, [2, 8], n_variants=1, n_rounds=1, n_sides=1)
    assert est3["per_scale_secs"][2] == 5.0
    assert est3["per_scale_secs"][8] == 20.0  # 5 * (8/2)
    assert est3["total_secs"] == 25.0
    print("#65b OK: estimate math (variants × scales × rounds × sides, linear in scale)")

    # --- (c) over-threshold abort; --accept-cost; dry-run; under-threshold ---
    big = tm.estimate_probe_matrix_secs(
        100.0, [1, 8], n_variants=5, n_rounds=4, n_sides=1)
    # 5 * (100+800) * 4 = 5*900*4 = 18000 > 14400
    assert big["total_secs"] == 18000.0

    buf = io.StringIO()
    try:
        tm.enforce_probe_cost_budget(
            big, max_est_secs=14400, accept_cost=False, dry_run=False,
            print_fn=lambda m: buf.write(m + "\n"))
        assert False, "over-threshold must SystemExit"
    except SystemExit as e:
        msg = str(e)
        assert "exceeds" in msg and "max-est-secs" in msg, msg
        assert "accept-cost" in msg or "Trim" in msg or "trim" in msg.lower(), msg
    assert "probe-lane cost estimate" in buf.getvalue()

    # --accept-cost proceeds loudly
    out_b = io.StringIO()
    err_b = io.StringIO()
    tm.enforce_probe_cost_budget(
        big, max_est_secs=14400, accept_cost=True, dry_run=False,
        print_fn=lambda m: out_b.write(m + "\n"),
        err_fn=lambda m: err_b.write(m + "\n"))
    assert "accept-cost" in err_b.getvalue(), err_b.getvalue()
    assert "probe-lane cost estimate" in out_b.getvalue()

    # dry-run prints estimate without aborting even when over threshold
    dry_b = io.StringIO()
    tm.enforce_probe_cost_budget(
        big, max_est_secs=100, accept_cost=False, dry_run=True,
        print_fn=lambda m: dry_b.write(m + "\n"))
    dry_out = dry_b.getvalue()
    assert "probe-lane cost estimate" in dry_out
    assert "dry-run" in dry_out

    # under-threshold: estimate printed, no abort, no accept-cost noise
    small = tm.estimate_probe_matrix_secs(
        1.0, [1, 8], n_variants=2, n_rounds=2, n_sides=1)
    assert small["total_secs"] < 14400
    under_b = io.StringIO()
    err_u = io.StringIO()
    tm.enforce_probe_cost_budget(
        small, max_est_secs=14400, accept_cost=False, dry_run=False,
        print_fn=lambda m: under_b.write(m + "\n"),
        err_fn=lambda m: err_u.write(m + "\n"))
    assert "probe-lane cost estimate" in under_b.getvalue()
    assert err_u.getvalue() == ""
    print("#65c OK: over-threshold abort; accept-cost; dry-run; under-threshold silent")

    # --- (d) preflight wired into run_calibrate / run_terminal ---------------
    sp_p = _sp(terminal_probe_workloads=1, terminal_probe_scales=[1, 8])

    def _icount(checkout, variant, scale):
        return 10000 * int(scale)

    # Over budget with injected base cost → SystemExit before full matrix
    try:
        tm.run_calibrate(
            sp_p, "/tmp/co-t49", rounds=2, skip_selfcheck=True,
            probe_factory=_fake_factory, probe_icount_runner=_icount,
            preflight_base_cost_secs=1000.0,  # huge → total >> 14400
            max_est_secs=60.0)
        assert False, "run_calibrate over budget must SystemExit"
    except SystemExit as e:
        assert "exceeds" in str(e), e

    # Under budget (tiny base) proceeds and writes floors
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        payload = tm.run_calibrate(
            sp_p, "/tmp/co-t49", rounds=2, skip_selfcheck=True,
            probe_factory=_fake_factory, probe_icount_runner=_icount,
            preflight_base_cost_secs=0.01,
            max_est_secs=14400,
            out_path=td / "floors-t49.json")
        assert Path(payload["path"]).is_file()
        assert payload["meta"]["terminal_lane"] == "probe"
        # scales [1,8] × 2 variants → 4 rows
        assert len(payload["floors"]) == 4, payload["floors"]

    # run_terminal: over budget aborts
    try:
        tm.run_terminal(
            sp_p, "/tmp/base-t49", "/tmp/cand-t49",
            rounds=1, floors={k: 0.5 for k in tm.probe_row_keys(
                _fake_factory(None), [1, 8])},
            skip_selfcheck=True,
            probe_factory=_fake_factory, probe_icount_runner=_icount,
            preflight_base_cost_secs=500.0, max_est_secs=30.0)
        assert False, "run_terminal over budget must SystemExit"
    except SystemExit as e:
        assert "exceeds" in str(e)

    # skip_cost_preflight still allows measure without budget check
    r = tm.run_terminal(
        sp_p, "/tmp/base-t49", "/tmp/cand-t49",
        rounds=1, floors={k: 0.5 for k in tm.probe_row_keys(
            _fake_factory(None), [1, 8])},
        skip_selfcheck=True,
        probe_factory=_fake_factory, probe_icount_runner=_icount,
        skip_cost_preflight=True)
    assert r.terminal_lane == "probe"
    print("#65d OK: preflight wired into calibrate/measure; skip path works")

    # --- (e) dry-run calibrate prints estimate table -------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spath = td / "t49.json"
        spath.write_text(json.dumps(_base_spec_dict(
            terminal_probe_scales=[1, 8], terminal_probe_workloads=1)))
        buf = io.StringIO()
        with redirect_stdout(buf):
            tm.calibrate_cli(SimpleNamespace(
                spec=str(spath), checkout="/tmp/co",
                rounds=2, dry_run=True,
                max_est_secs=14400, accept_cost=False,
                preflight_base_cost_secs=2.0,
                measure_bin=None))
        out = buf.getvalue()
        assert "terminal --calibrate dry-run" in out, out
        assert "probe-lane cost estimate" in out, out
        assert "total:" in out
        # dry-run must not abort even with tiny max_est
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            tm.calibrate_cli(SimpleNamespace(
                spec=str(spath), checkout="/tmp/co",
                rounds=4, dry_run=True,
                max_est_secs=1.0, accept_cost=False,
                preflight_base_cost_secs=10.0,
                measure_bin=None))
        out2 = buf2.getvalue()
        assert "probe-lane cost estimate" in out2
        assert "dry-run" in out2
    print("#65e OK: calibrate --dry-run prints estimate without aborting")

    # --- (f) docs greps: ONBOARDING quick-ref + Class A/B --------------------
    root = Path(__file__).resolve().parent.parent
    onboarding = (root / "docs" / "ONBOARDING.md").read_text()
    assert "New-target decisions" in onboarding
    for field in (
        "profile_fidelity",
        "terminal_lane",
        "terminal_probe_scales",
        "Toolchain pinning",
        "Editable regions",
        "Rounds budget",
    ):
        assert field in onboarding, f"ONBOARDING missing quick-ref row for {field!r}"

    operations = (root / "docs" / "OPERATIONS.md").read_text()
    campaign = (root / "skill" / "references" / "campaign-operator.md").read_text()
    for doc_name, text in (("OPERATIONS.md", operations),
                           ("campaign-operator.md", campaign)):
        assert "Class A" in text and "Class B" in text, doc_name
        assert "editing `targets/*.json` → Class B" in text, doc_name
        assert "editing `aro/*.py` → Class A" in text, doc_name
    print("#65f OK: docs carry quick-reference rows + Class A/B one-line test")

    print("case_65 OK: onboarding hardening (T49)")
