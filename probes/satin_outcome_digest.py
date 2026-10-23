#!/usr/bin/env python3
"""ARO differential for the Satin engine (mega-evm): the whole outcome of every fixture transaction.

Run by ARO as the target's external-command differential, once in the baseline worktree and once
in the candidate's, with the worktree as the working directory and its own CARGO_TARGET_DIR. ARO
compares the two `DIFF` lines byte for byte. Usage:

    satin_outcome_digest.py --fixtures DIR [--keep DIR] [--state-test BIN] [--no-btest]
    satin_outcome_digest.py --compare KEPT_A KEPT_B

What it does in the checkout:

1. builds `state-test` (`cargo build --release --locked -p state-test`, the CI runner);
2. runs the six whole-outcome digest runs of `tools/outcome-digest-diff.sh` — equivalence, satin
   and satin under tightened limits (`--limit-data-size 600 --limit-compute-gas 100`, so the
   limit-stop paths run across the corpus), each on Osaka (`DIR/main/state_tests`) and Amsterdam
   (`DIR/devnet/state_tests`) — with `--json-digest`: one line per executed transaction, the hash
   of its canonical outcome record (result, state, gas by ledger, usage, logs, oracle reads);
3. runs the blockchain tests of the main release through Satin's block executor (`state-test
   btest DIR/main/blockchain_tests --json-outcome`): one line per test, each block's gas used,
   bloom, receipts root and state root held to its header. The state tests call the EVM directly,
   so this run is what reaches `crates/mega-evm/src/block`;
4. fences the code no instrument compiles: every item of `crates/mega-evm/src` whose `cfg` names
   the `test-utils` feature. The benches, the probes and `state-test` all build mega-evm with
   `test-utils` on, a node builds it off, so an edit to either side of such a `cfg` is invisible
   to every run above (or measurable only in the instruments). Their text is folded in, so any
   edit to one, or a new one, changes the fingerprint.

Each run's lines are sorted (the runner is multi-threaded), the fixture root is replaced by
`$FIXTURES` (so the fingerprint does not depend on where the fixtures sit), and the run is folded
in as its line count, exit status and sha256. The `DIFF` line is the sha256 over all of them,
followed by a short token per part so a mismatch names the part that moved.

Exit status: 0 with a `DIFF` line; 1 without one when a run printed no line or a panic row (this
side cannot be vouched for, whichever side it is); 2 when the checkout, the build or the fixtures
are not usable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION = "satin-outcome-v1"
TIGHT = ["--limit-data-size", "600", "--limit-compute-gas", "100"]
# (name, state-test arguments with {main}/{devnet} for the fixture directories)
RUNS = [
    ("equivalence-Osaka", ["--mode", "equivalence", "--fork", "Osaka", "{main}/state_tests"]),
    ("equivalence-Amsterdam", ["--mode", "equivalence", "--fork", "Amsterdam",
                               "{devnet}/state_tests"]),
    ("satin-Osaka", ["--mode", "satin", "--fork", "Osaka", "{main}/state_tests"]),
    ("satin-Amsterdam", ["--mode", "satin", "--fork", "Amsterdam", "{devnet}/state_tests"]),
    ("tight-Osaka", ["--mode", "satin", "--fork", "Osaka", "{main}/state_tests", *TIGHT]),
    ("tight-Amsterdam", ["--mode", "satin", "--fork", "Amsterdam", "{devnet}/state_tests",
                         *TIGHT]),
]
BTEST = ("btest-Osaka", ["btest", "{main}/blockchain_tests"])
PANIC = re.compile(r'"(row|kind)":"panic"')
FENCE_ROOT = "crates/mega-evm/src"
FENCE_CFG = re.compile(r'#\[cfg\((?=[^\]]*"test-utils")|cfg!\((?=[^)]*"test-utils")')


def fail(status: int, message: str):
    print(f"satin-outcome-digest: {message}", file=sys.stderr, flush=True)
    sys.exit(status)


def build_state_test(checkout: Path) -> Path:
    cmd = ["cargo", "build", "--release", "--locked", "-p", "state-test",
           "--message-format=json-render-diagnostics"]
    out = subprocess.run(cmd, cwd=checkout, capture_output=True, text=True)
    if out.returncode != 0:
        tail = "\n".join(out.stderr.splitlines()[-40:])
        fail(2, f"building state-test failed (exit {out.returncode}):\n{tail}")
    for line in out.stdout.splitlines():
        if not line.startswith("{"):
            continue
        msg = json.loads(line)
        target = msg.get("target") or {}
        if (msg.get("reason") == "compiler-artifact" and msg.get("executable")
                and target.get("name") == "state-test" and "bin" in target.get("kind", [])):
            return Path(msg["executable"])
    fail(2, "cargo built state-test but named no executable for it")


def run(binary: Path, args: list, fixtures: Path, digest_flag: str):
    """One run: (sorted lines with the fixture root replaced, exit status, stdout summary)."""
    argv = [str(binary)] + [a.format(main=fixtures / "main", devnet=fixtures / "devnet")
                            for a in args] + [digest_flag]
    out = subprocess.run(argv, capture_output=True, text=True)
    root = str(fixtures)
    lines = sorted(line.replace(root, "$FIXTURES") for line in out.stderr.splitlines()
                   if line.startswith("{"))
    return lines, out.returncode, out.stdout


def fenced_items(checkout: Path) -> list:
    """`(path:line, text)` of every item under FENCE_ROOT whose cfg names `test-utils`.

    The item is the attribute through the end of what it annotates: the first `;` or `,` at
    bracket depth zero, the `}` that closes a block opened at depth zero, or the bracket that
    closes the enclosing one (a last field or arm). Brackets inside strings or comments may
    widen an item; that only makes the fence stricter, never blind.
    """
    items = []
    root = checkout / FENCE_ROOT
    for path in sorted(root.rglob("*.rs")):
        text = path.read_text()
        rel = path.relative_to(checkout).as_posix()
        for m in FENCE_CFG.finditer(text):
            # The head: `#[cfg(...)]` or `cfg!(...)`, through its closing bracket.
            i, depth = m.start(), 0
            while i < len(text):
                if text[i] in "([{":
                    depth += 1
                elif text[i] in ")]}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            # What it annotates.
            depth, opened = 0, False
            while i < len(text):
                c = text[i]
                if c in "([{":
                    depth += 1
                    opened = opened or (c == "{" and depth == 1)
                elif c in ")]}":
                    depth -= 1
                    if depth < 0:
                        break
                    if depth == 0 and c == "}" and opened:
                        i += 1
                        break
                elif c in ";," and depth == 0:
                    i += 1
                    break
                i += 1
            line = text.count("\n", 0, m.start()) + 1
            items.append((f"{rel}:{line}", text[m.start():i]))
    return items


def compare(kept_a: Path, kept_b: Path, show: int = 3) -> int:
    """Which kept runs differ between two `--keep` directories, and the first ids that do."""
    status = 0
    names = sorted({p.name for p in kept_a.glob("*.sorted")} | {p.name for p in kept_b.glob("*.sorted")})
    for name in names:
        a, b = kept_a / name, kept_b / name
        if not a.exists() or not b.exists():
            print(f"{name[:-7]}: only in {'B' if not a.exists() else 'A'}")
            status = 1
            continue
        la, lb = a.read_text().splitlines(), b.read_text().splitlines()
        if la == lb:
            print(f"{name[:-7]}: equal ({len(la)} lines)")
            continue
        status = 1
        sa, sb = set(la), set(lb)
        only_a, only_b = sorted(sa - sb), sorted(sb - sa)
        print(f"{name[:-7]}: DIFFERENT ({len(only_a)} lines only in A, {len(only_b)} only in B)")
        for line in (only_a[:show] + only_b[:show]):
            row = json.loads(line)
            side = "A" if line in sa else "B"
            print(f"  {side}: {row.get('test', '?')} d={row.get('d')} g={row.get('g')} "
                  f"v={row.get('v')} {row.get('digest') or json.dumps(row.get('result'))[:120]}")
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fixtures", type=Path,
                    help="directory holding main/state_tests, main/blockchain_tests and "
                         "devnet/state_tests")
    ap.add_argument("--keep", type=Path, help="write each run's sorted lines here")
    ap.add_argument("--state-test", type=Path,
                    help="use this state-test binary instead of building one (manual runs)")
    ap.add_argument("--no-btest", action="store_true", help="skip the blockchain tests' run")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("KEPT_A", "KEPT_B"),
                    help="compare two --keep directories and exit")
    args = ap.parse_args()
    if args.compare:
        return compare(*args.compare)
    if not args.fixtures:
        ap.error("--fixtures is required")

    checkout = Path.cwd()
    if not (checkout / FENCE_ROOT).is_dir() or not (checkout / "crates/state-test").is_dir():
        fail(2, f"{checkout} is not a mega-evm checkout with crates/state-test")
    fixtures = args.fixtures.resolve()
    for sub in ("main/state_tests", "devnet/state_tests", "main/blockchain_tests"):
        if not (fixtures / sub).is_dir():
            fail(2, f"no {sub} under {fixtures} (fetch the fixtures first)")
    stamp = fixtures / "FIXTURES.txt"
    print(f"FIXTURES {stamp.read_text().split()[0] if stamp.exists() else 'unstamped'}",
          flush=True)

    binary = args.state_test or build_state_test(checkout)
    usage = subprocess.run([str(binary), "--help"], capture_output=True, text=True).stdout
    if "--json-digest" not in usage:
        fail(2, f"{binary} has no --json-digest")

    runs = [(name, a, "--json-digest") for name, a in RUNS]
    if not args.no_btest:
        runs.append((BTEST[0], BTEST[1], "--json-outcome"))
    if args.keep:
        args.keep.mkdir(parents=True, exist_ok=True)
    parts, refused = [], []
    for name, run_args, flag in runs:
        lines, rc, _ = run(binary, run_args, fixtures, flag)
        body = "".join(line + "\n" for line in lines)
        sha = hashlib.sha256(body.encode()).hexdigest()
        panics = sum(1 for line in lines if PANIC.search(line))
        print(f"RUN {name} lines={len(lines)} rc={rc} panics={panics} sha256={sha}", flush=True)
        if args.keep:
            (args.keep / f"{name}.sorted").write_text(body)
        if not lines:
            refused.append(f"{name} printed no line (exit {rc})")
        if panics:
            refused.append(f"{name} has {panics} panic rows")
        parts.append((name, len(lines), rc, sha))

    fence = fenced_items(checkout)
    fence_sha = hashlib.sha256(
        "".join(f"{where}\n{text}\n\0" for where, text in fence).encode()).hexdigest()
    print(f"RUN cfg-fence items={len(fence)} sha256={fence_sha} "
          f"({', '.join(where for where, _ in fence)})", flush=True)
    parts.append(("cfg-fence", len(fence), 0, fence_sha))
    if args.keep:
        (args.keep / "cfg-fence.txt").write_text(
            "".join(f"== {where}\n{text}\n" for where, text in fence))

    if refused:
        fail(1, "refusing to fingerprint this checkout: " + "; ".join(refused))
    canonical = json.dumps([VERSION, parts], separators=(",", ":"))
    total = hashlib.sha256(canonical.encode()).hexdigest()
    tokens = " ".join(f"{name}={n}:{rc}:{sha[:12]}" for name, n, rc, sha in parts)
    print(f"DIFF {VERSION} {total} {tokens}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
