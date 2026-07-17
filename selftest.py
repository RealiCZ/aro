"""Cargo-free self-test: 21 isolated case groups covering the deterministic core
(compounding, event log, judge math, prescreen, probe/workload factories,
permtree, CLI parsing seams) with mock targets. No cargo, no model, no network.
A failing group never masks the rest; the runner reports every failure.

Domain case implementations live under tests/; this file is the aggregate entry
point so `python3 selftest.py` stays the CI command.
"""
from __future__ import annotations

from tests.selftest_engine import case_01, case_06, case_20, case_37, case_45
from tests.selftest_llm import case_23, case_34
from tests.selftest_manifest import case_21, case_51, case_52, case_53
from tests.selftest_misc import (
    case_02, case_03, case_04, case_05, case_07, case_08, case_09,
    case_11, case_12, case_14, case_15, case_16, case_17, case_18, case_19,
    case_22, case_24, case_25, case_26, case_27, case_28, case_29, case_30,
)
from tests.selftest_ablate import case_42
from tests.selftest_cli import case_47
from tests.selftest_init import case_43
from tests.selftest_liveness import case_44
from tests.selftest_preflight import case_48
from tests.selftest_reverify import case_39
from tests.selftest_scope import case_46
from tests.selftest_selfcheck import case_33
from tests.selftest_terminal import (
    case_31, case_32, case_35, case_36, case_38, case_40, case_41, case_50,
)
from tests.selftest_vcs import case_49
from tests.selftest_ship import case_54
from tests.selftest_ship_conformance import case_55

CASES = [case_01, case_02, case_03, case_04, case_05, case_06, case_07, case_08, case_09, case_11, case_12, case_14, case_15, case_16, case_17, case_18, case_19, case_20, case_21, case_22, case_23, case_24, case_25, case_26, case_27, case_28, case_29, case_30, case_31, case_32, case_33, case_34, case_35, case_36, case_37, case_38, case_39, case_40, case_41, case_42, case_43, case_44, case_45, case_46, case_47, case_48, case_49, case_50, case_51, case_52, case_53, case_54, case_55]


def run():
    """Run every case group; a failure no longer masks the rest — all failures
    are collected and reported, exit 1 if any."""
    import traceback
    failures = []
    for case in CASES:
        try:
            case()
        except Exception:
            failures.append((case.__name__, traceback.format_exc()))
    if failures:
        for name, tb in failures:
            print(f"\n=== FAILED {name} ===\n{tb}")
        raise SystemExit(f"SELFTEST FAILED: {len(failures)}/{len(CASES)} case group(s)")
    print("SELFTEST PASSED")

if __name__ == "__main__":
    run()
