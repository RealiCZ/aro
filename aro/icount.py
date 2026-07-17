"""Instruction-count (Ir) gate: pure parsing, profile fidelity, coverage, and
adjudication helpers for Gate 1.5.

Valgrind/callgrind I/O lives in `target.icount`; this module stays hermetic and
unit-testable from fixture text. Spec: ARO_ICOUNT_GATE_PLAN §2 / §5.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

from .types import MetricDelta, Verdict

# Ir is deterministic under fixed codegen; ε only absorbs residual environment
# nondeterminism (and can be tightened to 0 once server-side calibration lands).
DEFAULT_EPSILON_PCT = 0.1

# Cache-miss columns used as the locality dual-evidence channel (same callgrind
# run with --cache-sim=yes). Same-direction improvement = fewer misses.
_CACHE_MISS_EVENTS = ("D1mr", "DLmr")

_LOCALITY_CATEGORIES = frozenset({"locality", "memory", "cache"})


@dataclass
class ICountResult:
    """One whole-process callgrind measurement of a probe binary."""
    ir: int
    events: dict = field(default_factory=dict)  # event name -> count
    profile_fingerprint: str = ""


# --- callgrind text parser ---------------------------------------------------

def parse_callgrind_totals(text: str) -> dict:
    """Parse callgrind text-format `events:` + `totals:` into {event: int}.

    Trailing zero-valued columns are omitted by callgrind — treat missing
    trailing columns as 0. A malformed numeric token raises ValueError (caller
    must treat the measurement as failed; never silent zero). Missing events/
    totals lines also raise.
    """
    events = None
    totals_toks = None
    for line in text.splitlines():
        if line.startswith("events:"):
            events = line.split(":", 1)[1].split()
        elif line.startswith("totals:"):
            totals_toks = line.split(":", 1)[1].split()
    if not events:
        raise ValueError("callgrind output missing 'events:' header")
    if totals_toks is None:
        raise ValueError("callgrind output missing 'totals:' line")

    out: dict = {}
    for i, name in enumerate(events):
        if i < len(totals_toks):
            tok = totals_toks[i]
            try:
                out[name] = int(tok)
            except ValueError:
                # Never silently coerce to 0 — a bad token means the run is unusable.
                print(f"icount: malformed callgrind totals token {tok!r} for "
                      f"event {name!r}", file=sys.stderr)
                raise ValueError(
                    f"malformed callgrind totals token {tok!r} for event {name!r}")
        else:
            out[name] = 0  # omitted trailing zero columns
    return out


# --- profile-fidelity guard (§5) ---------------------------------------------

_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")

# Modes for check_profile_fidelity. Default codspeed-ci matches historical
# CodSpeed CI adjudication (cargo default multi-CGU). repo-release is for
# targets whose production truth is the checked-in [profile.release].
PROFILE_FIDELITY_CODSPEED_CI = "codspeed-ci"
PROFILE_FIDELITY_REPO_RELEASE = "repo-release"
PROFILE_FIDELITY_MODES = frozenset({
    PROFILE_FIDELITY_CODSPEED_CI,
    PROFILE_FIDELITY_REPO_RELEASE,
})


def _toml_sections(text: str) -> dict:
    """Minimal section splitter for Cargo.toml profile blocks. Values keep raw
    RHS text (enough for codegen-units / lto detection + fingerprint hashing)."""
    sections: dict = {}
    cur = None
    buf: list = []
    for line in text.splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            if cur is not None:
                sections[cur] = "\n".join(buf)
            cur = m.group(1).strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        sections[cur] = "\n".join(buf)
    return sections


def _profile_kv(section_body: str) -> dict:
    """Parse `key = value` lines from a profile section body (ignores comments)."""
    kv = {}
    for line in section_body.splitlines():
        s = line.split("#", 1)[0].strip()
        if not s or "=" not in s:
            continue
        k, _, v = s.partition("=")
        kv[k.strip()] = v.strip()
    return kv


def _all_profile_kvs(cargo_toml_text: str) -> dict:
    """Map every `[profile.*]` section name → {key: raw RHS value}."""
    out = {}
    for name, body in _toml_sections(cargo_toml_text or "").items():
        if name == "profile" or name.startswith("profile."):
            out[name] = _profile_kv(body)
    return out


def _compare_profile_fingerprint(
    candidate_text: str, baseline_text: str,
) -> Optional[str]:
    """Comparative profile fidelity: any profile.* drift vs baseline → error.

    Names the section and the differing key (or section add/remove). No a-priori
    rejection of any value — the baseline's checked-in profile is the truth.
    """
    cand = _all_profile_kvs(candidate_text)
    base = _all_profile_kvs(baseline_text)
    # Deterministic order: sections then keys, alphabetically.
    for sec in sorted(set(cand) | set(base)):
        if sec not in base:
            return (f"profile-fidelity: [{sec}] section added in candidate "
                    "(absent in baseline)")
        if sec not in cand:
            return (f"profile-fidelity: [{sec}] section removed in candidate "
                    "(present in baseline)")
        ck, bk = cand[sec], base[sec]
        for key in sorted(set(ck) | set(bk)):
            if key not in bk:
                return (f"profile-fidelity: [{sec}] key '{key}' added in "
                        "candidate")
            if key not in ck:
                return (f"profile-fidelity: [{sec}] key '{key}' removed in "
                        "candidate")
            if ck[key] != bk[key]:
                return (f"profile-fidelity: [{sec}] key '{key}' differs "
                        f"(baseline={bk[key]}, candidate={ck[key]})")
    return None


def check_profile_fidelity(
    cargo_toml_text: str,
    mode: str = PROFILE_FIDELITY_CODSPEED_CI,
    baseline_cargo_toml_text: Optional[str] = None,
) -> Optional[str]:
    """Return an error message if the worktree is unsafe to measure; else None.

    Invariant: measurement build config == adjudication/production build config
    (and per-candidate untampered). Mode selects how that invariant is checked:

    * ``codspeed-ci`` (default): byte-identical to the historical a-priori checks —
      reject ``[profile.bench]`` ``codegen-units``/``lto`` overrides; reject
      ``[profile.release].codegen-units == 1``. Assumes production adjudication is
      cargo's default multi-CGU (CodSpeed CI). Other profile names (e.g.
      ``[profile.maxperf]``) are ignored — not the ``--release`` measurement profile.
    * ``repo-release``: comparative only. Fingerprint every ``profile.*`` section of
      the candidate Cargo.toml against the baseline worktree's; any drift (value
      changed, key added/removed, section added/removed) rejects with a message
      naming the section and differing key. No a-priori rejection of any value —
      the repo's checked-in profile is the measurement truth (e.g. salt's CGU=1
      + thin LTO + panic=abort production release).

    Anti-tamper: Cargo.toml is outside every spec's editable regions, so a
    candidate editing it is already guard-rejected — this fingerprint check is
    belt-and-braces at the measurement seam (and catches operator-side drift too).

    ``baseline_cargo_toml_text`` is required for ``repo-release`` (ignored for
    ``codspeed-ci``).
    """
    mode = (mode or PROFILE_FIDELITY_CODSPEED_CI).strip()
    if mode == PROFILE_FIDELITY_REPO_RELEASE:
        if baseline_cargo_toml_text is None:
            return ("profile-fidelity: repo-release mode requires baseline "
                    "Cargo.toml text for comparison")
        return _compare_profile_fingerprint(
            cargo_toml_text, baseline_cargo_toml_text)

    # codspeed-ci (default): historical a-priori checks — keep messages
    # byte-identical for every existing target.
    # [profile.bench] is checked even though the Ir probe builds with --release:
    # the same candidate later flows through criterion/codspeed terminal
    # validation, which builds with the bench profile — so bench-profile
    # codegen knobs in the worktree signal a tainted measurement environment.
    sections = _toml_sections(cargo_toml_text)
    bench = _profile_kv(sections.get("profile.bench", ""))
    if "codegen-units" in bench:
        return ("profile-fidelity: [profile.bench] overrides codegen-units "
                f"(={bench['codegen-units']}) — measurement-only knobs are forbidden")
    if "lto" in bench:
        return ("profile-fidelity: [profile.bench] overrides lto "
                f"(={bench['lto']}) — measurement-only knobs are forbidden")
    rel = _profile_kv(sections.get("profile.release", ""))
    cgu = rel.get("codegen-units")
    if cgu is not None:
        try:
            if int(cgu) == 1:
                return ("profile-fidelity: [profile.release] codegen-units = 1 "
                        "distorts instruction counts vs production multi-CGU builds")
        except ValueError:
            pass  # non-int RHS: leave for fingerprint; don't hard-reject
    return None


def profile_fingerprint(cargo_toml_text: str, rustc_version: str) -> str:
    """Stable fingerprint: rustc -V + hash of [profile.release]/[profile.bench].

    Attached to every lesson/permtree record that passed through the Ir gate so
    later 'same opt, two conclusions' diffs can be attributed to config drift.
    """
    sections = _toml_sections(cargo_toml_text)
    parts = []
    for name in ("profile.release", "profile.bench"):
        body = sections.get(name, "")
        # Normalize whitespace for a stable hash across formatting noise.
        norm = "\n".join(l.rstrip() for l in body.splitlines() if l.strip())
        parts.append(f"[{name}]\n{norm}")
    h = hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]
    rustc = (rustc_version or "").strip() or "rustc-unknown"
    return f"{rustc}|{h}"


# --- coverage precheck (§2.4) ------------------------------------------------

def probe_covers_patch(probe_covers, patched_files) -> bool:
    """True when at least one patched file path overlaps a probe_covers prefix."""
    if not patched_files:
        return True  # NoOp control: nothing to cover, don't NO_COVERAGE it
    prefixes = list(probe_covers or [])
    if not prefixes:
        return True
    for f in patched_files:
        f = f.replace("\\", "/")
        for p in prefixes:
            p = (p or "").replace("\\", "/").rstrip("/")
            if not p:
                continue
            if f == p or f.startswith(p + "/") or f.startswith(p):
                return True
    return False


def warn_if_no_probe_covers(probe_covers) -> None:
    if not probe_covers:
        print("WARNING: target has no probe_covers; Ir-gate coverage precheck "
              "skipped (set probe_covers in the target JSON for NO_COVERAGE "
              "protection)", file=sys.stderr)


# --- locality + adjudication -------------------------------------------------

def is_locality_claim(candidate) -> bool:
    """Minimal seam: Candidate.category defaults to 'cpu'; locality/memory/cache
    declare the wall-clock exception channel."""
    cat = (getattr(candidate, "category", None) or "cpu").strip().lower()
    return cat in _LOCALITY_CATEGORIES


def ir_epsilon_pct(spec=None) -> float:
    """ε in percent. Env ARO_ICOUNT_EPSILON wins over target JSON icount_epsilon_pct."""
    env = os.environ.get("ARO_ICOUNT_EPSILON")
    if env is not None and env != "":
        return float(env)
    if spec is not None:
        v = getattr(spec, "icount_epsilon_pct", None)
        if v is not None:
            return float(v)
        raw = getattr(spec, "raw", None) or {}
        if "icount_epsilon_pct" in raw:
            return float(raw["icount_epsilon_pct"])
    return DEFAULT_EPSILON_PCT


def cache_evidence_improves(base_events: dict, cand_events: dict) -> bool:
    """Same-direction cache-miss improvement: at least one miss column drops and
    none rises. Missing columns (cache-sim off) → False."""
    improved_any = False
    for ev in _CACHE_MISS_EVENTS:
        b, c = base_events.get(ev), cand_events.get(ev)
        if b is None or c is None:
            return False
        if c > b:
            return False
        if c < b:
            improved_any = True
    return improved_any


def ir_delta_pct(base_ir: int, cand_ir: int) -> float:
    """Whole-process ΔIr% = (cand − base) / base * 100.

    Iteration counts are identical on both sides (same ARO_BENCH_SCALE), so the
    ratio is directly comparable without per-call normalization. Caveat: fixed
    process-startup Ir dilutes the percentage for short workloads — prefer a
    scale that still exercises the hot path under valgrind.
    """
    if base_ir == 0:
        raise ValueError("baseline Ir is 0 — measurement unusable")
    return (cand_ir - base_ir) / base_ir * 100.0


@dataclass
class IrGateDecision:
    """Result of Gate 1.5 adjudication.

    `passthrough` True means continue into wall-clock Gate 2 (locality channel).
    Otherwise `verdict` is terminal.
    """
    passthrough: bool
    verdict: Optional[Verdict] = None
    ir_delta_pct: float = 0.0
    notes: list = field(default_factory=list)
    deltas: list = field(default_factory=list)


def _ir_delta_obj(base_ir: int, cand_ir: int, delta: float, eps: float,
                  improved: bool, regressed: bool) -> MetricDelta:
    # Deterministic signal: CI collapses to the point estimate; floor = ε.
    return MetricDelta(
        metric="Ir",
        baseline=float(base_ir),
        candidate=float(cand_ir),
        delta_pct=delta,
        ci_low_pct=delta,
        ci_high_pct=delta,
        floor_pct=eps,
        improved=improved,
        regressed=regressed,
        noise_limited=False,
        bench_scale=1,
    )


def judge_ir(base: ICountResult, cand: ICountResult, *, epsilon_pct: float,
             locality: bool) -> IrGateDecision:
    """Adjudicate one Ir A/B pair.

    Δ < −ε → ACCEPTED_IR
    Δ > +ε → REGRESSED_IR
    |Δ| ≤ ε → NEUTRAL_IR for cpu; locality + cache evidence → passthrough to Gate 2
    """
    delta = ir_delta_pct(base.ir, cand.ir)
    notes = [f"Ir gate: base={base.ir} cand={cand.ir} Δ={delta:+.4f}% ε={epsilon_pct}%"]
    if delta < -epsilon_pct:
        d = _ir_delta_obj(base.ir, cand.ir, delta, epsilon_pct, True, False)
        notes.append("verdict: accepted-ir — Ir improved beyond ε")
        return IrGateDecision(False, Verdict.ACCEPTED_IR, delta, notes, [d])
    if delta > epsilon_pct:
        d = _ir_delta_obj(base.ir, cand.ir, delta, epsilon_pct, False, True)
        notes.append("verdict: regressed-ir — Ir regressed beyond ε")
        return IrGateDecision(False, Verdict.REGRESSED_IR, delta, notes, [d])
    # Inside ε band.
    if locality:
        if cache_evidence_improves(base.events, cand.events):
            notes.append("Ir within ε; locality claim + cache-miss improvement → "
                         "passthrough to wall-clock significance gate")
            return IrGateDecision(True, None, delta, notes, [])
        notes.append("verdict: neutral-ir — locality claim but no cache-miss "
                     "evidence (D1mr/DLmr); discarding")
        d = _ir_delta_obj(base.ir, cand.ir, delta, epsilon_pct, False, False)
        return IrGateDecision(False, Verdict.NEUTRAL_IR, delta, notes, [d])
    notes.append("verdict: neutral-ir — |ΔIr| ≤ ε (product likely unchanged / "
                 "compiler already did it)")
    d = _ir_delta_obj(base.ir, cand.ir, delta, epsilon_pct, False, False)
    return IrGateDecision(False, Verdict.NEUTRAL_IR, delta, notes, [d])
