"""CodSpeed confirm layer: the target's own CodSpeed benches must agree with the probe.

A per-candidate gate between the inner loop and the terminal gate, on when the spec sets
`codspeed_confirm.enabled`. The inner loop accepts on the probe (whole-probe Ir, or a
wall-clock A/B); a probe can be over-fitted, so before an accepted candidate may fold into the
baseline (and later reach the terminal gate) its claim is checked on the instrument CI
adjudicates with:

1. **Probe rows.** The bench probe is row-aware: its rows are named by the CodSpeed row keys of
   the workloads they reproduce (`ARO_PROBE_LIST=1` lists them). Each row's instruction count is
   measured on the baseline and the candidate (`SpecTarget.icount_rows`: callgrind collecting
   inside the routine only). A row whose count falls by more than ε (`icount_epsilon_pct`) is a
   **claimed** row; a row that moves by more than ε either way is **affected**.
2. **CodSpeed rows.** The bench targets holding the affected rows (a row's target is its first
   `/` segment when that names one of `terminal_bench_targets`; otherwise every target) are
   built with `cargo codspeed build` and run with `cargo codspeed run` in simulation mode —
   through `mega-bench-reporter measure --instructions`, the terminal gate's instrument — on the
   baseline and on the candidate, `rounds` times each (median per row).
3. **Agreement.** Confirmed only when every claimed row improves in CodSpeed (Δ < 0) and no
   subject row of the measured targets regresses past its floor (the terminal floors file, else
   `terminal_default_floor_pct`). Control-lane rows are not judged. No claimed row at all is a
   disagreement too: the probe's acceptance names no row CodSpeed could confirm.

Anything else — a disagreement, or a confirm that could not measure — turns the accept into
`codspeed-unconfirmed`: the candidate is frozen for review (its patch stays in the run, the
`codspeed_confirm` event carries both sides' counts per row), never folded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

CONFIRMED = "confirmed"
DISAGREED = "disagreed"
ERRORED = "errored"


@dataclass
class ConfirmConfig:
    rounds: int
    probe_iters: int
    timeout_secs: Optional[float]
    epsilon_pct: float
    package: str
    bench_targets: list
    control_lanes: list


@dataclass
class ConfirmResult:
    status: str
    reasons: list = field(default_factory=list)
    rows: dict = field(default_factory=dict)      # row -> per-row evidence (see judge)
    claimed: list = field(default_factory=list)
    affected: list = field(default_factory=list)
    targets: list = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.status == CONFIRMED

    def to_event(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons),
                "claimed": list(self.claimed), "affected": list(self.affected),
                "targets": list(self.targets), "rows": dict(self.rows)}


def config(spec) -> Optional[ConfirmConfig]:
    """The confirm settings for `spec`, or None when the layer is off."""
    cc = getattr(spec, "codspeed_confirm", None) or {}
    if not cc.get("enabled"):
        return None
    from . import icount as icmod
    from . import terminal as tm
    return ConfirmConfig(
        rounds=int(cc.get("rounds") or 1),
        probe_iters=int(cc.get("probe_iters") or 8),
        timeout_secs=cc.get("timeout_secs") or tm.resolve_terminal_timeout(spec),
        epsilon_pct=icmod.ir_epsilon_pct(spec),
        package=tm.package_name(spec),
        bench_targets=list(getattr(spec, "terminal_bench_targets", None) or []),
        control_lanes=tm.resolve_control_lanes(spec),
    )


def pct(base, cand) -> Optional[float]:
    if not base:
        return None
    return (float(cand) - float(base)) / float(base) * 100.0


def target_of(row: str, bench_targets: list) -> Optional[str]:
    """The bench target a row key belongs to: its first segment when that names a target."""
    head = str(row).split("/", 1)[0]
    return head if head in bench_targets else None


def judge(probe_base: dict, probe_cand: dict, cs_base: dict, cs_cand: dict, *,
          epsilon_pct: float, floor_of: Callable[[str], float],
          control_lanes=(), targets=()) -> ConfirmResult:
    """The pure decision over measured counts (both dicts per side: row -> count).

    `cs_*` hold the CodSpeed rows of the measured targets. Returns a result whose `rows`
    maps every probe row and every judged CodSpeed row to its evidence: the counts on both
    sides, Δ%, the floor, and whether the row was claimed."""
    from .terminal import is_control_row
    reasons, rows = [], {}
    if set(probe_base) != set(probe_cand):
        reasons.append(f"probe row sets differ: only baseline {sorted(set(probe_base) - set(probe_cand))}, "
                       f"only candidate {sorted(set(probe_cand) - set(probe_base))}")
    claimed, affected = [], []
    for row in sorted(set(probe_base) & set(probe_cand)):
        d = pct(probe_base[row], probe_cand[row])
        rows[row] = {"probe_base": probe_base[row], "probe_cand": probe_cand[row],
                     "probe_delta_pct": d, "claimed": d is not None and d <= -epsilon_pct}
        if d is not None and abs(d) > epsilon_pct:
            affected.append(row)
        if rows[row]["claimed"]:
            claimed.append(row)
    if not claimed:
        reasons.append(f"no probe row improves by more than ε={epsilon_pct}%: the inner "
                       "loop's acceptance names no row CodSpeed could confirm")

    for row in claimed:
        if row not in cs_base or row not in cs_cand:
            reasons.append(f"claimed row {row} is not in the CodSpeed output (row names out "
                           "of step with the benches?)")
    for row in sorted(set(cs_base) & set(cs_cand)):
        if is_control_row(row, control_lanes):
            continue
        d = pct(cs_base[row], cs_cand[row])
        floor = float(floor_of(row))
        ev = rows.setdefault(row, {})
        ev.update({"codspeed_base": cs_base[row], "codspeed_cand": cs_cand[row],
                   "codspeed_delta_pct": d, "floor_pct": floor})
        if row in claimed and not (d is not None and d < 0):
            reasons.append(f"claimed row {row}: probe Δ{rows[row]['probe_delta_pct']:+.4f}% but "
                           f"CodSpeed Δ{d:+.4f}% (did not improve)")
        if d is not None and d > floor:
            reasons.append(f"row {row} regresses in CodSpeed: Δ{d:+.4f}% > floor {floor}%")
    only = sorted(set(cs_base) ^ set(cs_cand))
    if only:
        reasons.append(f"CodSpeed row sets differ between the sides: {only}")
    return ConfirmResult(status=DISAGREED if reasons else CONFIRMED, reasons=reasons,
                         rows=rows, claimed=claimed, affected=affected, targets=list(targets))


def _measure_env(target, work) -> dict:
    """The environment the CodSpeed build runs in: the caller's, minus ARO's own
    profile overrides (CodSpeed CI builds with the repo's profile), with a target dir of
    the worktree's own — two worktrees sharing one would measure a stale path dependency."""
    env = dict(os.environ)
    for key in ("CARGO_PROFILE_RELEASE_DEBUG", "CARGO_PROFILE_RELEASE_STRIP"):
        env.pop(key, None)
    env["CARGO_TARGET_DIR"] = str(Path(target.td_for(work)) / "codspeed-confirm")
    return env


def _env_runner(env: dict):
    import subprocess

    def run(cmd, timeout=None):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.stdout, p.stderr, p.returncode
    return run


def run_confirm(target, baseline_work, work, *, cfg: ConfirmConfig,
                measure_runner_for: Optional[Callable] = None,
                measure_bin: Optional[str] = None) -> ConfirmResult:
    """Measure both sides and judge. Never raises: a failure is an `errored` result
    carrying whatever was measured. `measure_runner_for(work)` injects the measure
    subprocess (tests); `measure_bin` overrides the spec/env resolution (tests)."""
    from . import terminal as tm
    spec = getattr(target, "spec", None)
    probe_base, probe_cand, targets = {}, {}, []
    try:
        rows = target.probe_rows(work)
        probe_base = target.icount_rows(baseline_work, rows, cfg.probe_iters)
        probe_cand = target.icount_rows(work, rows, cfg.probe_iters)
        eps = cfg.epsilon_pct
        affected = [r for r in rows
                    if (d := pct(probe_base.get(r), probe_cand.get(r))) is not None
                    and abs(d) > eps]
        targets = sorted({t for r in affected
                          if (t := target_of(r, cfg.bench_targets)) is not None})
        if not targets or any(target_of(r, cfg.bench_targets) is None for r in affected):
            targets = list(cfg.bench_targets)
        mb = measure_bin or tm.resolve_measure_bin(spec)
        docs = {}
        for side, w in (("baseline", baseline_work), ("candidate", work)):
            runner = (measure_runner_for(w) if measure_runner_for
                      else _env_runner(_measure_env(target, w)))
            docs[side] = tm.measure_checkout_rounds(
                w, package=cfg.package, bench_targets=targets, measure_bin=mb,
                rounds=cfg.rounds, timeout=cfg.timeout_secs, runner=runner)
        floors, _meta, _warn = tm.load_floors(getattr(spec, "name", "") or "")
        default_floor = tm.resolve_default_floor_pct(spec)
        result = judge(probe_base, probe_cand, docs["baseline"].rows, docs["candidate"].rows,
                       epsilon_pct=eps,
                       floor_of=lambda r: float(floors.get(r, default_floor)),
                       control_lanes=cfg.control_lanes, targets=targets)
        if docs["baseline"].profile_fingerprint != docs["candidate"].profile_fingerprint:
            result.status = DISAGREED
            result.reasons.append(
                "CodSpeed profile fingerprints differ between the sides "
                f"({docs['baseline'].profile_fingerprint} vs "
                f"{docs['candidate'].profile_fingerprint}): the candidate changed the build")
        return result
    except Exception as e:  # the instrument could not answer: not a confirmation
        rows = {r: {"probe_base": probe_base.get(r), "probe_cand": probe_cand.get(r)}
                for r in sorted(set(probe_base) | set(probe_cand))}
        return ConfirmResult(status=ERRORED, reasons=[f"confirm could not measure: {e}"],
                             rows=rows, targets=targets)
