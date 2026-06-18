"""Autonomously find a target's real hot path + measure the isolated kernel.

Observe-only (changes nothing): profile the kernel under macOS `sample` to see
where time actually goes, and measure the isolated microbench directly. Reuses the
SAME `SpecTarget` plumbing the loop uses — per-worktree `CARGO_TARGET_DIR` and
`cargo metadata` crate-dir resolution — instead of a second copy of the cargo glue,
so it works for any workspace layout (e.g. a crate under `crates/`):

    python3 find_hotpath.py targets/<name>.json
"""
from __future__ import annotations

import sys

from aro import profile
from aro import spec as specmod
from aro.stats import median
from aro.target import SpecTarget

if len(sys.argv) < 2:
    raise SystemExit("usage: python3 find_hotpath.py <targets/spec.json>")
SPEC = specmod.load(sys.argv[1])


def main():
    b = SPEC.bench
    target = SpecTarget(SPEC)
    work = target.make_worktree("hotpath")
    try:
        print(f"building + measuring isolated kernel ({b['metric']}) ...")
        m = target.bench(work)            # SpecTarget writes the probe into the
        samples = m.get(b["metric"]) or []  # right crate dir + builds in its own td
        ns = median(samples) if samples else None
        if ns is None:
            print("FAILED to measure: probe produced no "
                  f"'{b['sample_prefix']}' samples")
            return

        p = SPEC.profile
        binary = target._td_for(work) / "release" / "examples" / \
            p.get("example", b["example"])
        print("profiling the kernel with macOS `sample` ...")
        funcs = profile.top_functions(binary, spin_secs=p.get("spin_secs", 8),
                                      sample_secs=p.get("sample_secs", 4))

        print("\n" + "=" * 64)
        print(f"STEP 2 — isolated kernel benchmark ({b['metric']})")
        print(f"    median = {ns:.1f}")
        print("\nSTEP 1 — autonomous hot path (sample, in-binary compute frames)")
        if not funcs:
            print("    (no profile parsed)")
        for name, c, pct in funcs:
            print(f"    {pct:5.1f}%  {name:<24} ({c} samples)")
        print("=" * 64)
        if funcs:
            f = SPEC.context.get("file", "(see spec regions)")
            print(f"\n>>> hottest function: {funcs[0][0]}  (file: {f})")
    finally:
        target.remove_worktree(work)


if __name__ == "__main__":
    main()
