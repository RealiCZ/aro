#!/usr/bin/env python3
"""Check that a row-aware bench probe's rows are exactly the target's CodSpeed subject rows.

    codspeed_rowmap.py --spec targets/<name>.json [--checkout DIR]

Run against a checkout of the target (default: the spec's `target_repo.path`), at the commit
the campaign runs on. It

1. lists the rows every bench target in `terminal_bench_targets` declares, with criterion's own
   `--list` (`cargo bench -p <pkg> --bench <target> -- --list`): `group/function_id`, the same
   key `mega-bench-reporter` gives the CodSpeed row (`group/subject/workload`);
2. builds the spec's bench probe as the example ARO builds and lists its rows (`ARO_PROBE_LIST=1`);
3. requires the probe's rows to equal the bench rows minus the control lanes (`control_lanes`):
   every probe row has a CodSpeed row to confirm it, and every subject row has a probe row.

The probe file is copied into the checkout's examples for the build and removed afterwards.
Exit status: 0 the maps agree, 1 they do not, 2 a build or listing failed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(status, message):
    print(f"codspeed-rowmap: {message}", file=sys.stderr)
    sys.exit(status)


def bench_rows(checkout: Path, pkg: str, target: str) -> list:
    out = subprocess.run(["cargo", "bench", "-q", "-p", pkg, "--bench", target, "--", "--list"],
                         cwd=checkout, capture_output=True, text=True)
    if out.returncode != 0:
        fail(2, f"listing bench {target} failed (exit {out.returncode}):\n"
                + "\n".join(out.stderr.splitlines()[-20:]))
    return [line[: -len(": benchmark")] for line in out.stdout.splitlines()
            if line.endswith(": benchmark")]


def probe_rows(checkout: Path, pkg: str, probe: Path, example: str) -> list:
    meta = subprocess.run(["cargo", "metadata", "--format-version", "1", "--no-deps"],
                          cwd=checkout, capture_output=True, text=True, check=True)
    pkg_dir = next(Path(p["manifest_path"]).parent for p in json.loads(meta.stdout)["packages"]
                   if p["name"] == pkg)
    dest = pkg_dir / "examples" / f"{example}.rs"
    if dest.exists():
        fail(2, f"{dest} exists already; refusing to overwrite it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    made_dir = not any(dest.parent.iterdir())
    dest.write_text(probe.read_text())
    try:
        build = subprocess.run(["cargo", "build", "--release", "-p", pkg, "--example", example,
                                "--message-format=json-render-diagnostics"],
                               cwd=checkout, capture_output=True, text=True)
        if build.returncode != 0:
            fail(2, "building the probe failed:\n" + "\n".join(build.stderr.splitlines()[-20:]))
        exe = None
        for line in build.stdout.splitlines():
            if line.startswith("{"):
                msg = json.loads(line)
                if (msg.get("reason") == "compiler-artifact" and msg.get("executable")
                        and msg.get("target", {}).get("name") == example):
                    exe = msg["executable"]
        listed = subprocess.run([exe], cwd=checkout, capture_output=True, text=True,
                                env={"ARO_PROBE_LIST": "1", "PATH": "/usr/bin:/bin"})
        rows = next((line.split()[1:] for line in listed.stdout.splitlines()
                     if line.startswith("ROWS ")), None)
        if rows is None:
            fail(2, "the probe has no row mode (ARO_PROBE_LIST=1 printed no ROWS line)")
        return rows
    finally:
        dest.unlink()
        if made_dir:
            dest.parent.rmdir()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--checkout", type=Path)
    args = ap.parse_args()
    spec = json.loads(args.spec.read_text())
    checkout = (args.checkout or Path(spec["target_repo"]["path"])).resolve()
    pkg = spec["benchmark_probe"]["pkg"]
    targets = spec.get("terminal_bench_targets") or []
    controls = set(spec.get("control_lanes") or [])
    if not targets:
        fail(2, "the spec declares no terminal_bench_targets")

    declared = []
    for t in targets:
        rows = bench_rows(checkout, pkg, t)
        print(f"bench {t}: {len(rows)} rows")
        declared.extend(rows)
    subject = [r for r in declared if not controls & set(r.split("/"))]
    probe = probe_rows(checkout, pkg, ROOT / spec["benchmark_probe"]["probe"],
                       spec["benchmark_probe"]["example"])
    print(f"CodSpeed rows: {len(declared)} ({len(declared) - len(subject)} control, "
          f"{len(subject)} subject); probe rows: {len(probe)}")
    missing = sorted(set(subject) - set(probe))
    extra = sorted(set(probe) - set(subject))
    for row in sorted(set(subject) & set(probe)):
        print(f"  {row}")
    if missing or extra:
        for row in missing:
            print(f"  NO PROBE ROW for CodSpeed row {row}")
        for row in extra:
            print(f"  NO CODSPEED ROW for probe row {row}")
        print("rowmap: DIFFERENT")
        return 1
    print("rowmap: equal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
