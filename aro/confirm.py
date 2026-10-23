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
   subject row of the measured targets regresses past its floor. A row's floor is its calibrated
   one (the terminal floors file), else ε: an uncalibrated row gets the probe's own resolution,
   not the terminal's 1% pre-calibration default. A row of a protected family
   (`protected_row_families`) is capped further at the protected band for ε,
   `max(ε + margin_pp, floor_multiple × ε)` from `protected_hysteresis`, whatever its calibrated
   floor: the terminal lets no trade touch those rows, so the confirm lets no regression through
   them. Control-lane rows are not judged. No claimed row at all is a disagreement too, decided
   before CodSpeed is run: the probe's acceptance names no row CodSpeed could confirm.

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
    protected_families: list = field(default_factory=list)
    hysteresis: Optional[dict] = None


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
        protected_families=list(tm.resolve_protected_row_families(spec) or []),
        hysteresis=tm.resolve_protected_hysteresis(spec),
    )


def row_floor(row: str, floors: dict, cfg: ConfirmConfig) -> tuple:
    """`(floor %, source)` a row may regress by in CodSpeed before the confirm disagrees: the
    calibrated floor, else ε; for a protected family, at most the protected band for ε."""
    from .terminal import hysteresis_ceiling, row_family
    if row in floors:
        floor, source = float(floors[row]), "calibrated"
    else:
        floor, source = float(cfg.epsilon_pct), "epsilon"
    if row_family(row) in cfg.protected_families:
        cap = hysteresis_ceiling(cfg.epsilon_pct, cfg.hysteresis)
        if cap < floor:
            floor, source = cap, source + "+protected-cap"
        else:
            source += "+protected"
    return floor, source


def pct(base, cand) -> Optional[float]:
    if not base:
        return None
    return (float(cand) - float(base)) / float(base) * 100.0


def target_of(row: str, bench_targets: list) -> Optional[str]:
    """The bench target a row key belongs to: its first segment when that names a target."""
    head = str(row).split("/", 1)[0]
    return head if head in bench_targets else None


def judge(probe_base: dict, probe_cand: dict, cs_base: dict, cs_cand: dict, *,
          epsilon_pct: float, floor_of: Callable[[str], object],
          control_lanes=(), targets=()) -> ConfirmResult:
    """The pure decision over measured counts (both dicts per side: row -> count).

    `cs_*` hold the CodSpeed rows of the measured targets; `floor_of(row)` gives the row's
    floor %, or `(floor %, source)`. Returns a result whose `rows` maps every probe row and
    every judged CodSpeed row to its evidence: the counts on both sides, Δ%, the floor (and
    where it came from), and whether the row was claimed."""
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
        got = floor_of(row)
        floor, source = (float(got[0]), got[1]) if isinstance(got, tuple) else (float(got), None)
        ev = rows.setdefault(row, {})
        ev.update({"codspeed_base": cs_base[row], "codspeed_cand": cs_cand[row],
                   "codspeed_delta_pct": d, "floor_pct": floor})
        if source:
            ev["floor_source"] = source
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
        if not any((d := pct(probe_base.get(r), probe_cand.get(r))) is not None and d <= -eps
                   for r in rows):
            # Nothing claimed: the answer is known before any CodSpeed run (tens of
            # minutes under valgrind) — a disagreement on the probe rows alone.
            result = judge(probe_base, probe_cand, {}, {}, epsilon_pct=eps,
                           floor_of=lambda r: eps, control_lanes=cfg.control_lanes)
            result.reasons.append("CodSpeed not run: there is no claimed row to confirm")
            return result
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
        result = judge(probe_base, probe_cand, docs["baseline"].rows, docs["candidate"].rows,
                       epsilon_pct=eps, floor_of=lambda r: row_floor(r, floors, cfg),
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
