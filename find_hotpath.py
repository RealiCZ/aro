"""Autonomously find a target's real hot path + measure the isolated kernel.

Observe-only (changes nothing): profile the kernel under macOS `sample` to see
where time actually goes, and measure the isolated microbench directly. Reads the
SAME TargetSpec the loop uses, so it works for any target — not just salt:

    python3 find_hotpath.py [targets/<name>.json]   # default: salt-committer
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from aro import profile
from aro import spec as specmod

SPEC = specmod.load(sys.argv[1] if len(sys.argv) > 1
                    else Path(__file__).parent / "targets" / "salt-committer.json")
REPO = SPEC.repo
TARGET_DIR = (REPO.parent / ".aro-salt-target").resolve()


def main():
    b = SPEC.bench
    wt = REPO.parent / ".aro-worktrees" / "hotpath"
    subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                   capture_output=True)
    shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt),
                    SPEC.baseline_ref], check=True, capture_output=True)
    try:
        ex = wt / b["pkg"] / "examples" / f"{b['example']}.rs"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text(SPEC.probe_src())

        env = dict(os.environ)
        env["CARGO_TARGET_DIR"] = str(TARGET_DIR)
        print(f"building + measuring isolated kernel ({b['metric']}) ...")
        out = subprocess.run(
            ["cargo", "run", "--release", "-p", b["pkg"], "--example", b["example"]],
            cwd=str(wt), env=env, capture_output=True, text=True)
        ns = None
        for line in out.stdout.splitlines():
            if line.startswith(b["sample_prefix"]):
                vals = sorted(float(x) for x in line.split()[1:])
                if vals:
                    ns = vals[len(vals) // 2]
        if ns is None:
            print("FAILED to measure; stderr tail:\n" + "\n".join(out.stderr.splitlines()[-15:]))
            return

        p = SPEC.profile
        binary = TARGET_DIR / "release" / "examples" / p.get("example", b["example"])
        print("profiling the kernel with macOS `sample` ...")
        funcs = profile.top_functions(binary, spin_secs=p.get("spin_secs", 8),
                                      sample_secs=p.get("sample_secs", 4))

        print("\n" + "=" * 64)
        print(f"STEP 2 — isolated kernel benchmark ({b['metric']})")
        print(f"    median = {ns:.1f} ns/call")
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
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)],
                       capture_output=True)


if __name__ == "__main__":
    main()
