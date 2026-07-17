"""Hermetic tests for selftest auto-discovery (replaces central CASES registry).

This module's case_58 is itself discovered with no registration step — proof of the mechanism.
"""

from __future__ import annotations

import sys
import types as _types


def _fake_mod(name: str, **attrs) -> _types.ModuleType:
    """Create a fake module object populated with the given attrs (for collect tests)."""
    m = _types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def case_58():
    """T41: selftest case auto-discovery tests (collect, sort, dups, end-to-end self-presence)."""
    main = sys.modules.get("__main__")
    collect = getattr(main, "_collect_cases", None)
    discover = getattr(main, "_discover_selftest_cases", None)
    assert collect is not None, "_collect_cases helper missing from __main__"
    assert discover is not None, "_discover_selftest_cases missing from __main__"

    # 1. collect helper: fake modules -> correct collection; excludes case_10x / helper_case_* / non-callable
    def case_01(): pass
    def case_02(): pass
    def case_10x(): pass  # invalid name pattern
    def helper_case_1(): pass  # not ^case_\d+$
    noncall = 123

    fake1 = _fake_mod(
        "fake1",
        case_01=case_01,
        case_02=case_02,
        case_10x=case_10x,
        helper_case_1=helper_case_1,
        noncallable=noncall,
        other=lambda: None,
    )
    collected = collect([(fake1, "fake1")])
    assert [c.__name__ for c in collected] == ["case_01", "case_02"], \
        f"collect excluded wrong names; got {[c.__name__ for c in collected]}"

    # 2. numeric sort (case_2 before case_10)
    def case_02(): pass
    def case_10(): pass
    fake_sort = _fake_mod("fake_sort", case_10=case_10, case_02=case_02)
    coll_sorted = collect([(fake_sort, "fake_sort")])
    assert [c.__name__ for c in coll_sorted] == ["case_02", "case_10"], \
        f"numeric sort failed; got {[c.__name__ for c in coll_sorted]}"

    # 3. duplicate number across two fake modules -> SystemExit naming both locations
    def case_07(): pass
    fake_a = _fake_mod("moda", case_07=case_07)
    fake_b = _fake_mod("modb", case_07=case_07)
    try:
        collect([(fake_a, "moda"), (fake_b, "modb")])
        assert False, "expected SystemExit on duplicate case number"
    except SystemExit as exc:
        msg = str(exc)
        assert "Duplicate case number 7" in msg, f"bad dup msg: {msg}"
        assert "moda" in msg and "modb" in msg, f"dup msg must name both modules: {msg}"

    # 4. end-to-end live discovery: finds case_58 itself + total count >= current suite size (incl. prior 55 + this)
    live = discover()
    names = [c.__name__ for c in live]
    assert "case_58" in names, f"live discovery missed case_58 (self); discovered: {names}"
    assert len(live) >= 56, f"discovered only {len(live)} cases; expected >=56 (old suite + case_58)"

    print("case_58 OK: discovery collect/sort/dup/live-self")