"""Shared helpers/fixtures for selftest domain modules."""
from __future__ import annotations

import types as _types

from aro.types import Metrics
from aro import attempt as _at, frontier as _fr, report_md as _rm, symbols as _sy

FAST = "src/opt.rs"  # an edit on this path makes the mock bench ~5% faster

# Shared across case groups: the split-module namespace shim
# (sweep's pure helpers now live in symbols/frontier/report_md/attempt).
_sw = _types.SimpleNamespace(
    classify_owner=_sy.classify_owner, _demangle_leaf=_sy._demangle_leaf,
    bucket_functions=_fr.bucket_functions, _grep_fn_files=_fr._grep_fn_files,
    _refill_queue=_fr._refill_queue, _addressable=_fr._addressable,
    _floor_pct=_fr._floor_pct, _split_headroom=_fr._split_headroom,
    _explore_decision=_fr._explore_decision,
    render_map=_rm.render_map, render_explore_report=_rm.render_explore_report,
    render_attempt_map=_rm.render_attempt_map,
    _summarize_report=_at._summarize_report, _seed_memory=_at._seed_memory,
    _probe_rescue=_at._probe_rescue)


class MockTarget:
    """In-memory target. bench() gets faster for each FAST edit applied to a
    worktree, so a candidate carrying it is 'accepted' and compounding triggers."""
    name = "mock"

    def __init__(self):
        self._wt = {}          # live worktree -> list of applied edit paths
        self.apply_log = []    # permanent (work, path) history
        self._tick = 0

    def objectives(self):
        return []

    def make_worktree(self, tag):
        self._tick += 1
        p = f"/tmp/mock-wt-{tag}-{self._tick}"
        self._wt[p] = []
        return p

    def remove_worktree(self, work):
        self._wt.pop(work, None)

    def apply(self, patch, work):
        for e in patch.edits:
            self._wt.setdefault(work, []).append(e.path)
            self.apply_log.append((work, e.path))

    def build(self, work):
        pass

    def test(self, work):
        pass

    def differential(self, work, baseline):
        return True

    def bench(self, work, scale=1):
        n_fast = self._wt.get(work, []).count(FAST)
        base = 100.0 * (0.95 ** n_fast)        # each FAST edit shaves ~5%
        self._tick += 1
        jit = ((self._tick % 5) - 2) * 0.01    # tiny deterministic jitter
        m = Metrics()
        m.put("metric/x", [base + jit, base, base - jit])
        return m
