"""Cargo-free self-test: isolated case groups covering the deterministic core
(compounding, event log, judge math, prescreen, probe/workload factories,
permtree, CLI parsing seams, ship gates, ...) with mock targets. No cargo, no model, no network.
A failing group never masks the rest; the runner reports every failure.

ADD TESTS: create tests/selftest_<domain>.py containing def case_<N>(): ... — nothing to register.
Cases are auto-discovered by name (module-level callables matching ^case_\\d+$).
Duplicate case numbers across any selftest_*.py (or this file) cause SystemExit naming both locations.
This file is the aggregate entry point so `python3 selftest.py` stays the zero-dependency CI command.

Domain case implementations live under tests/.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import sys
import traceback
import types as _types


def _is_case_name(name: str) -> bool:
    return bool(re.match(r"^case_\d+$", name))


def _collect_cases(mods: list[tuple[_types.ModuleType, str]]) -> list[callable]:
    """Hermetic collect helper: given [(module, label), ...] return sorted case funcs.

    Only module-level callables named ^case_\\d+$ are kept.
    case_10x, helper_case_*, and non-callables are excluded.
    Numeric sort ensures case_2 before case_10.
    Duplicate numbers across modules -> SystemExit naming both locations.
    """
    discovered: list[tuple[int, callable]] = []
    seen: dict[int, str] = {}
    for mod, mod_label in mods:
        for attr in dir(mod):
            if not _is_case_name(attr):
                continue
            func = getattr(mod, attr)
            if not callable(func):
                continue
            num = int(attr.split("_", 1)[1])
            if num in seen:
                raise SystemExit(f"Duplicate case number {num} in {seen[num]} and {mod_label}")
            seen[num] = mod_label
            discovered.append((num, func))
    discovered.sort(key=lambda t: t[0])
    return [f for _, f in discovered]


def _discover_selftest_cases() -> list[callable]:
    """Discovery: pkgutil.iter_modules over tests package (name filter selftest_*),
    plus selftest.py's own module (if it defines any case_*).

    Imports ONLY tests/selftest_*.py modules — no other modules under tests/.
    Any import error in a selftest_*.py is a loud failure (SystemExit), never skipped.
    """
    mods: list[tuple[_types.ModuleType, str]] = []

    # Use pkgutil over the tests package, but import only matching selftest_ modules.
    try:
        tests_pkg = importlib.import_module("tests")
    except Exception as e:
        raise SystemExit(f"Failed to import tests package for discovery: {e}") from e

    prefix = tests_pkg.__name__ + "."
    for _importer, modname, ispkg in pkgutil.iter_modules(tests_pkg.__path__, prefix):
        if ispkg:
            continue
        short = modname.rsplit(".", 1)[-1]
        if not short.startswith("selftest_"):
            continue
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            raise SystemExit(f"Failed to import {modname}: {e}") from e
        mods.append((mod, mod.__name__))

    # Own module (selftest.py): supports cases defined directly here (covers __main__ script run and `import selftest`).
    if "selftest" in sys.modules:
        mods.append((sys.modules["selftest"], "selftest"))
    if "__main__" in sys.modules:
        main_mod = sys.modules["__main__"]
        if not any(main_mod is m for m, _ in mods):
            mods.append((main_mod, main_mod.__name__))

    return _collect_cases(mods)


def run():
    """Run every case group; a failure no longer masks the rest — all failures
    are collected and reported, exit 1 if any."""
    cases = _discover_selftest_cases()
    failures = []
    for case in cases:
        try:
            case()
        except Exception:
            failures.append((case.__name__, traceback.format_exc()))
    if failures:
        for name, tb in failures:
            print(f"\n=== FAILED {name} ===\n{tb}")
        raise SystemExit(f"SELFTEST FAILED: {len(failures)}/{len(cases)} case group(s)")
    print("SELFTEST PASSED")


if __name__ == "__main__":
    run()
