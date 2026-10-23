"""The Satin target (targets/mega-evm-satin.json) and its outcome-digest differential.

The lessons the gate encodes, pinned so a later edit of the spec cannot quietly drop them: the
differential is the execution-spec outcome digest (not a random-input probe), the inner test gate
is not `--lib` alone, the full gate denies warnings as CI does, and the instruments' own code is
not editable. The digest driver (probes/satin_outcome_digest.py) runs here against a fake
`state-test`: hermetic, no cargo, no fixtures download.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "targets/mega-evm-satin.json"
DRIVER = ROOT / "probes/satin_outcome_digest.py"


def case_74():
    """Satin target: the spec keeps the gate's lessons."""
    from aro import spec as specmod
    print("=== case 74: mega-evm-satin spec invariants ===")
    sp = specmod.load(SPEC)
    raw = json.loads(SPEC.read_text())
    assert sp.baseline_ref == "2e482f2239cb883ccc1f553add5eaeeccf8058d2", sp.baseline_ref
    assert sp.llm_backend == "codex"

    # The differential is the outcome digest, run as a command; never weak.
    d = sp.differential
    assert d["probe"] == "probes/satin_outcome_digest.py" and d["prefix"] == "DIFF", d
    assert d["command"][:3] == ["python3", "-I", "{probe}"] and "--fixtures" in d["command"], d
    assert "--no-btest" not in d["command"], "the block executor's run must stay in the gate"
    assert not sp.constraints.get("weak_oracle")

    # Inner gate: the whole package's tests, not the library's alone; locked like CI.
    assert sp.test[:2] == ["cargo", "test"] and "--lib" not in sp.test, sp.test
    assert "-p" in sp.test and "mega-evm" in sp.test and "--locked" in sp.test, sp.test
    # Full gate: the workspace, release, locked, warnings denied (CI's RUSTFLAGS).
    full = raw["correctness_oracle"]["test_full"]
    assert full[:2] == ["env", "RUSTFLAGS=-D warnings"], full
    for tok in ("--workspace", "--release", "--locked"):
        assert tok in full, (tok, full)

    # Editable: inside crates/mega-evm/src, never the instruments' code (test-utils modules).
    for region in sp.regions:
        assert region.startswith("crates/mega-evm/src/"), region
        assert "test_utils" not in region and "hasher" not in region, region
    assert sp.constraints["no_new_deps"] and sp.constraints["byte_identical"]

    # Certification lanes: the op-revm arms are the controls; realistic families protected.
    assert sp.control_lanes == ["op_revm"], sp.control_lanes
    assert set(raw["protected_row_families"]) == {"block", "corpus"}
    assert set(sp.terminal_bench_targets) == {"transact", "corpus", "block", "factory"}
    assert raw["pinned_tools"]["rustc"] == "1.97.0-nightly"
    print("case_74 OK: mega-evm-satin spec invariants")


# A stand-in for `state-test`: prints the fixture files it finds as digest lines on stderr, in
# reverse order (the real runner is multi-threaded), shaped by marker files in the checkout:
# `mutate` changes one satin-mode line, `panic` adds a panic row, `silent` prints nothing.
_FAKE = r'''#!{python}
import hashlib, json, sys
from pathlib import Path
args = sys.argv[1:]
if "--help" in args:
    print("--json-digest  --json-outcome")
    sys.exit(0)
checkout = Path.cwd()
btest = args[:1] == ["btest"]
mode = "btest" if btest else args[args.index("--mode") + 1]
tight = "--limit-data-size" in args
paths = [a for a in args if a.startswith("/")]
files = sorted(p for d in paths for p in Path(d).rglob("*.json"))
lines = []
for f in files:
    row = {{"mode": mode, "path": str(f), "test": f.stem, "d": 0, "g": 0, "v": 0,
           "digest": "0x" + hashlib.sha256(f"{{mode}}{{tight}}{{f.name}}".encode()).hexdigest()[:8],
           "row": "outcome"}}
    if (checkout / "mutate").exists() and mode == "satin" and f.name == "b.json":
        row["digest"] = "0xdead"
    lines.append(json.dumps(row, separators=(",", ":")))
if (checkout / "panic").exists() and mode == "equivalence":
    lines.append(json.dumps({{"test": "boom", "row": "panic"}}, separators=(",", ":")))
if (checkout / "silent").exists():
    lines = []
for line in reversed(lines):
    sys.stderr.write(line + "\n")
print("summary on stdout, not a digest line")
'''


def _fixtures(root: Path) -> Path:
    for sub in ("main/state_tests", "devnet/state_tests", "main/blockchain_tests"):
        (root / sub).mkdir(parents=True)
        for name in ("a.json", "b.json", "c.json"):
            (root / sub / name).write_text("{}")
    return root


def _checkout(root: Path) -> Path:
    src = root / "crates/mega-evm/src"
    src.mkdir(parents=True)
    (root / "crates/state-test").mkdir(parents=True)
    (src / "lib.rs").write_text(
        'pub mod evm;\n#[cfg(any(test, feature = "test-utils"))]\npub mod test_utils;\n')
    (src / "evm.rs").write_text(
        "pub fn hot(x: u64) -> u64 {\n    x + 1\n}\n"
        "pub fn reset(s: &mut S) {\n"
        '    #[cfg(any(test, feature = "test-utils"))]\n    {\n        s.a = s.b;\n    }\n'
        '    #[cfg(not(any(test, feature = "test-utils")))]\n    {\n        s.a = None;\n    }\n'
        '    #[cfg(not(any(test, feature = "test-utils")))]\n    let neutral = false;\n'
        "}\n")
    return root


def _drive(checkout: Path, fixtures: Path, fake: Path, *extra):
    out = subprocess.run(
        [sys.executable, "-I", str(DRIVER), "--fixtures", str(fixtures), "--state-test",
         str(fake), *extra], cwd=checkout, capture_output=True, text=True, timeout=120)
    diff = [line for line in out.stdout.splitlines() if line.startswith("DIFF ")]
    return out.returncode, (diff[0] if diff else None), out


def case_75():
    """Satin target: the outcome-digest driver fingerprints, refuses, and fences."""
    print("=== case 75: satin_outcome_digest.py against a fake state-test ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fake = tmp / "state-test"
        fake.write_text(_FAKE.format(python=sys.executable))
        fake.chmod(0o755)
        fx_a, fx_b = _fixtures(tmp / "fx-a"), _fixtures(tmp / "elsewhere/fx-b")
        base, cand = _checkout(tmp / "base"), _checkout(tmp / "cand")

        rc, fp, out = _drive(base, fx_a, fake)
        assert rc == 0 and fp, (rc, out.stdout, out.stderr)
        assert fp.startswith("DIFF satin-outcome-v1 "), fp
        for part in ("equivalence-Osaka=3:0:", "tight-Amsterdam=3:0:", "btest-Osaka=3:0:",
                     "cfg-fence=4:0:"):
            assert part in fp, (part, fp)
        # Same checkout, fixtures elsewhere: the fixture root is not part of the fingerprint.
        rc, fp_b, _ = _drive(base, fx_b, fake)
        assert rc == 0 and fp_b == fp, (fp, fp_b)
        # An identical candidate matches.
        rc, fp_c, _ = _drive(cand, fx_a, fake)
        assert fp_c == fp

        # A behaviour change in satin mode moves the satin and tight runs, and only those.
        (cand / "mutate").write_text("")
        rc, fp_m, _ = _drive(cand, fx_a, fake, "--keep", str(tmp / "kept-cand"))
        assert rc == 0 and fp_m != fp
        moved = {t.split("=")[0] for t in fp_m.split()[3:]} - {t.split("=")[0]
                                                                  for t in fp.split()[3:]}
        changed = {t.split("=")[0] for t in set(fp_m.split()[3:]) ^ set(fp.split()[3:])}
        assert not moved and changed == {"satin-Osaka", "satin-Amsterdam", "tight-Osaka",
                                         "tight-Amsterdam"}, changed
        (cand / "mutate").unlink()
        # --compare names the run and the test that moved.
        _drive(base, fx_a, fake, "--keep", str(tmp / "kept-base"))
        cmp = subprocess.run([sys.executable, "-I", str(DRIVER), "--compare",
                              str(tmp / "kept-base"), str(tmp / "kept-cand")],
                             capture_output=True, text=True, timeout=60)
        assert cmp.returncode == 1, cmp.stdout
        assert "satin-Osaka: DIFFERENT" in cmp.stdout and "equivalence-Osaka: equal" in cmp.stdout
        assert " b d=0" in cmp.stdout, cmp.stdout

        # A panic row or a silent run: no fingerprint at all, exit 1.
        for marker, why in (("panic", "panic rows"), ("silent", "printed no line")):
            (cand / marker).write_text("")
            rc, fp_x, out = _drive(cand, fx_a, fake)
            assert rc == 1 and fp_x is None and why in out.stderr, (marker, rc, out.stderr)
            (cand / marker).unlink()

        # The fence: an edit to a test-utils-gated item (either side) moves cfg-fence; an edit
        # to ordinary code does not (the runs judge that).
        evm = cand / "crates/mega-evm/src/evm.rs"
        text = evm.read_text()
        evm.write_text(text.replace("x + 1", "x + 2"))
        rc, fp_o, _ = _drive(cand, fx_a, fake)
        assert fp_o == fp, "ordinary code is the runs' business, not the fence's"
        for edited in (text.replace("s.a = None;", "s.a = Some(0);"),
                       text.replace("let neutral = false;", "let neutral = true;"),
                       text.replace("s.a = s.b;", "s.a = s.b.take();"),
                       text + '#[cfg(feature = "test-utils")]\npub fn fast() {}\n'):
            evm.write_text(edited)
            rc, fp_f, _ = _drive(cand, fx_a, fake)
            assert rc == 0 and fp_f != fp, edited
            assert [t for t in fp_f.split() if t.startswith("cfg-fence=")] != \
                [t for t in fp.split() if t.startswith("cfg-fence=")]
        evm.write_text(text)

        # Not a checkout, or no fixtures: exit 2, no fingerprint.
        rc, fp_n, _ = _drive(tmp, fx_a, fake)
        assert rc == 2 and fp_n is None
        rc, fp_n, _ = _drive(base, tmp / "nope", fake)
        assert rc == 2 and fp_n is None
    print("case_75 OK: outcome-digest driver fingerprints, refuses, and fences")


def case_76():
    """Satin target: the hand-back bundle round-trips through base64 with patches and evidence."""
    import base64
    import tarfile
    from aro import patchfile
    from aro.types import Edit, Patch
    print("=== case 76: satin_handback.py packs a run ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src/hot.rs").write_text('fn hot() -> &\'static str {\n    "a \\"quoted\\" str"\n}\n')
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env,
                           capture_output=True)
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
        run = tmp / "satin-hot"
        (run / "a1/patches").mkdir(parents=True)
        p1 = Patch([Edit("src/hot.rs", '"a \\"quoted\\" str"', '"b \\"quoted\\" str"')])
        p2 = Patch([Edit("src/hot.rs", "fn hot()", "#[inline]\nfn hot()")])
        (run / "a1/patches/c1.txt").write_text(patchfile.dump(p1))
        (run / "a1/patches/c2.txt").write_text(patchfile.dump(p2))
        entry = {"attempt": "a1", "fn": "hot", "files": ["src/hot.rs"], "regime":
                 "byte-identical", "critic_verdict": "pass", "mergeable": False}
        (run / "manifest.json").write_text(json.dumps({
            "spec": "mega-evm-satin", "baseline_ref": base, "run_id": "r1", "accepted": [
                {**entry, "order": 2, "id": "c2", "delta_pct": -0.8,
                 "patch_path": "a1/patches/c2.txt", "hypothesis": "inline hot"},
                {**entry, "order": 1, "id": "c1", "delta_pct": -1.5,
                 "patch_path": "a1/patches/c1.txt", "hypothesis": "cheaper string"}]}))
        (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"event": "gate", "candidate": "c1", "gate": "differential", "status": "ok",
             "fingerprint": "DIFF satin-outcome-v1 abc"},
            {"event": "gate", "candidate": "c2", "gate": "test", "status": "ok"},
            {"event": "gate", "candidate": "zz", "gate": "test", "status": "fail"}]))

        out = subprocess.run([sys.executable, "-I", str(ROOT / "probes/satin_handback.py"),
                              str(run), "--repo", str(repo)], capture_output=True, text=True,
                             timeout=120)
        assert out.returncode == 0, out.stderr
        assert "2 accepted entries packed" in out.stdout, out.stdout
        tgz, b64 = tmp / "satin-hot-handback.tgz", tmp / "satin-hot-handback.tgz.b64"
        assert base64.decodebytes(b64.read_bytes()) == tgz.read_bytes()
        with tarfile.open(tgz) as tar:
            files = {m.name: m for m in tar.getmembers()}
            read = lambda n: tar.extractfile(files[f"satin-hot-handback/{n}"]).read().decode()
            assert '+    "b \\"quoted\\" str"' in read("entries/01-c1.patch")
            assert "+#[inline]" in read("entries/02-c2.patch")
            assert read("stack.patch").count("Subject: [PATCH") == 2
            assert "differential" in read("evidence/01-c1.jsonl")
            assert "zz" not in read("evidence/02-c2.jsonl")
            summary = read("SUMMARY.txt")
            assert "#1 c1" in summary and "differential=DIFF satin-outcome-v1 abc" in summary
            assert "#2 c2" in summary and "differential=NO GATE EVENT" in summary
            assert read("a1/patches/c1.txt".join(["patches/", ""])) == patchfile.dump(p1)
        wts = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                             text=True).stdout.strip().splitlines()
        assert len(wts) == 1, wts
    print("case_76 OK: hand-back bundle round-trips with patches and evidence")
