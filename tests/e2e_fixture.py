#!/usr/bin/env python3
"""Cargo fixture E2E — the safety net for the real judge path.

Drives the FULL real chain on fixtures/mini-target (a tiny crate with a known,
byte-identical, order-of-magnitude win): git worktree → cargo build → cargo test →
random-input differential → A/A floor calibration → paired A/B → manifest.

Three legs:
  A. the seeded WIN patch (hoist an i-independent inner loop) must come back
     `accepted` and fold into the baseline;
  B. a seeded BEHAVIOUR-CHANGING patch that still passes the unit tests
     (`i % 63` → `i % 62`, unit tests only cover i < 2) must be killed by the
     DIFFERENTIAL gate — the exact reason the differential exists;
  C. `manifest.build_manifest` over leg A's out-dir lists exactly that win.

Skips (exit 0) when cargo is unavailable. Pure stdlib; safe for CI.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aro import spec as specmod                      # noqa: E402
from aro.engine import run_backtest                  # noqa: E402
from aro.events import EventLog                      # noqa: E402
from aro.generator import PlannedGenerator           # noqa: E402
from aro.manifest import build_manifest              # noqa: E402
from aro.store import Memory                         # noqa: E402
from aro.target import SpecTarget                    # noqa: E402
from aro.types import Edit, Verdict                  # noqa: E402

SLOW = """    let mut acc = 0u64;
    for i in 0..xs.len() {
        let mut base = 0u64;
        for j in 0..xs.len() {
            base = base.wrapping_add(xs[j] ^ (j as u64));
        }
        acc = acc.wrapping_add(base.rotate_left((i % 63) as u32) ^ xs[i]);
    }
    acc"""

FAST = """    let mut base = 0u64;
    for j in 0..xs.len() {
        base = base.wrapping_add(xs[j] ^ (j as u64));
    }
    let mut acc = 0u64;
    for i in 0..xs.len() {
        acc = acc.wrapping_add(base.rotate_left((i % 63) as u32) ^ xs[i]);
    }
    acc"""

# Behaviour-changing but unit-test-passing: rotate amounts identical for i < 62,
# and the unit tests only exercise len <= 2. Only the random-input differential
# (len up to 200) can catch this — which is precisely what leg B asserts.
SNEAKY = SLOW.replace("(i % 63)", "(i % 62)")


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    shutil.copytree(REPO_ROOT / "fixtures" / "mini-target", repo,
                    ignore=shutil.ignore_patterns("probes", "target"))
    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True, timeout=60)
    git("init", "-q")
    git("config", "user.email", "aro-e2e@example.invalid")
    git("config", "user.name", "aro-e2e")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture baseline")
    return repo


def make_spec(repo: Path):
    return specmod.from_dict({
        "name": "fixture-mini",
        "target_repo": {"path": str(repo), "baseline_ref": "HEAD"},
        "hot_path": {"file": "src/lib.rs", "fn": "checksum"},
        "metric": "ns_per_call",
        "direction": "minimize",
        "benchmark_probe": {
            "probe": "fixtures/mini-target/probes/mini_target.rs",
            "example": "aro_bench", "pkg": "mini-target",
            "profile": {"spin_secs": 1, "sample_secs": 1},
        },
        "correctness_oracle": {
            "build": ["cargo", "build", "--release", "-p", "mini-target"],
            "test": ["cargo", "test", "--release", "-p", "mini-target"],
            "differential": {
                "probe": "fixtures/mini-target/probes/mini_target_diff.rs",
                "pkg": "mini-target", "example": "aro_diff", "prefix": "DIFF",
            },
        },
        "constraints": {"editable": ["src/lib.rs"]},
        "run": {"aa_runs": 2, "ab_pairs": 3, "bench_scales": [1],
                "read_phase": False, "timeout": 600,
                "stop": {"max_rounds": 1, "dry_rounds": 1}},
    })


def run_one(spec, out: Path, edits, cand_id: str):
    out.mkdir(parents=True, exist_ok=True)
    target = SpecTarget(spec)
    gen = PlannedGenerator([(cand_id, f"seeded fixture patch {cand_id}", edits)])
    events = EventLog(out / "events.jsonl", also_console=False)
    return run_backtest(
        target, gen, Memory(out),
        rounds=1, candidates_per_round=1,
        aa_runs=spec.aa_runs, ab_pairs=spec.ab_pairs,
        baseline_ref=spec.baseline_ref, events=events,
        goal=spec.goal, stop_dry_rounds=1, read_phase=False,
        bench_scales=spec.bench_scales)


def main() -> int:
    if shutil.which("cargo") is None:
        print("SKIP: cargo not available — fixture E2E needs a Rust toolchain")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="aro-e2e-"))
    try:
        repo = make_repo(tmp)
        spec = make_spec(repo)

        # --- leg A: the seeded WIN must be accepted and folded --------------
        win = [Edit(path="src/lib.rs", search=SLOW, replace=FAST)]
        rep = run_one(spec, tmp / "outA", win, "win")
        assert len(rep.outcomes) == 1, f"expected 1 outcome, got {len(rep.outcomes)}"
        cand, out = rep.outcomes[0]
        assert out.verdict == Verdict.ACCEPTED, \
            f"win patch not accepted: {out.verdict.value} — {out.notes}"
        assert rep.folded_edits, "accepted win was not folded into the baseline"
        assert rep.floors.floors, "A/A floors were not calibrated"
        print(f"leg A OK: win accepted ({out.notes[-1] if out.notes else ''})")

        # --- leg B: behaviour change that unit tests miss → differential ----
        sneaky = [Edit(path="src/lib.rs", search=SLOW, replace=SNEAKY)]
        repB = run_one(spec, tmp / "outB", sneaky, "sneaky")
        assert len(repB.outcomes) == 1
        _, outB = repB.outcomes[0]
        assert outB.verdict == Verdict.VERIFY_FAILED, \
            f"sneaky patch must be verify-failed, got {outB.verdict.value} — {outB.notes}"
        assert any("differential" in n for n in outB.notes), \
            f"sneaky patch must die at the DIFFERENTIAL gate, notes: {outB.notes}"
        assert not repB.folded_edits, "behaviour-changing patch must never fold"
        print("leg B OK: unit-test-invisible behaviour change killed by the differential")

        # --- leg C: manifest over leg A ---------------------------------------
        m = build_manifest(tmp / "outA")
        assert len(m["accepted"]) == 1, f"manifest accepted={len(m['accepted'])}"
        entry = m["accepted"][0]
        assert entry["files"] == ["src/lib.rs"], entry
        assert entry["delta_pct"] is not None and entry["delta_pct"] < -50, \
            f"expected a huge win, got Δ={entry['delta_pct']}"
        # plain `aro run` has no attempt/regime context → conservatively NOT mergeable
        assert entry["mergeable"] is False, entry
        print(f"leg C OK: manifest lists the win (Δ={entry['delta_pct']:+.1f}%)")

        # --- leg D: probe-factory qualification through REAL cargo ------------
        # A canned micro-probe (the parent bench source itself — already tight on
        # checksum) goes through Q1/Q2/Q4 with real builds/benches; Q3 relevance is
        # injected (CI runners have no profiler) — Q3's gate logic is covered by
        # selftest #28 with fakes and by macOS runs with the real sampler.
        from aro import probe_factory as pf
        from aro.types import Objective
        probe_rel = pf.probe_rel_path(spec.name, "checksum")
        probe_abs = REPO_ROOT / probe_rel
        probe_abs.parent.mkdir(parents=True, exist_ok=True)
        # A genuinely TIGHTER probe than the parent (more averaging per sample).
        # Q2 compares two LIVE A/A calibrations, so on a machine with bursty load
        # it can legitimately fail — that is the gate doing its job, not a bug.
        # Policy here: escalate the probe once; if the ONLY failing gate is still
        # Q2, note-and-skip (the deterministic gate logic is pinned by selftest
        # #28; leg D's unique value is the real-cargo Q1/Q4 mechanics).
        parent_src = (REPO_ROOT / "fixtures/mini-target/probes/mini_target.rs").read_text()
        try:
            q = None
            for reps in (400, 4000):
                probe_abs.write_text(parent_src.replace(
                    "let reps = 40 * scale;", f"let reps = {reps} * scale;"))
                q = pf.qualify(spec, "checksum", probe_rel,
                               parent_floors=rep.floors,
                               objectives=[Objective("ns_per_call", True)],
                               aa_runs=2,
                               profile_shares=lambda s: {"checksum": 90.0})
                if q.ok:
                    break
                if not all(r.startswith("Q2") for r in q.reasons):
                    break   # a non-noise gate failed — that IS a real E2E failure
            if q.ok:
                assert q.sha256 and 0.6 <= q.scale_ratio <= 3.5, q
                print(f"leg D OK: probe qualified (floor {q.floor_pct:.2f}% vs parent "
                      f"{q.parent_floor_pct:.2f}%, scale ratio {q.scale_ratio:.2f})")
            elif all(r.startswith("Q2") for r in q.reasons):
                print(f"leg D SKIP: Q2 floor-gain not reachable under current machine "
                      f"load ({q.reasons[0]}) — gate logic is selftest-pinned (#28)")
            else:
                raise AssertionError(f"probe qualification failed: {q.reasons}")
        finally:
            probe_abs.unlink(missing_ok=True)

        print("FIXTURE E2E PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
