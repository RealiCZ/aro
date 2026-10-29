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
    assert sp.baseline_ref == "5e3f30ad4692f3815105dea1368f789f032ddd2b", sp.baseline_ref
    assert sp.llm_backend == "codex"

    # The differential is the outcome digest, run as a command; never weak.
    d = sp.differential
    assert d["probe"] == "probes/satin_outcome_digest.py" and d["prefix"] == "DIFF", d
    assert d["command"][:3] == ["python3", "-I", "{probe}"] and "--fixtures" in d["command"], d
    assert "--no-btest" not in d["command"], "the block executor's run must stay in the gate"
    # Every run's line count is pinned (CI's counts at the pin): a short corpus fails the gate.
    pins = dict(p.split("=") for p in d["command"][d["command"].index("--expect-lines") + 1]
                .split(","))
    assert pins == {"equivalence-Osaka": "19465", "equivalence-Amsterdam": "15754",
                    "satin-Osaka": "19465", "satin-Amsterdam": "15754", "tight-Osaka": "19465",
                    "tight-Amsterdam": "15754", "btest-Osaka": "21558"}, pins
    assert not sp.constraints.get("weak_oracle")

    # Phase 2 pins the baseline's two fingerprints in the target file: the outcome digest and the
    # read set it now carries. A later campaign that silently re-pins them (or drops the read set
    # out of the DIFF line) breaks here.
    pinned = d["pinned"]
    assert pinned["baseline_ref"] == sp.baseline_ref, pinned
    assert pinned["diff"].startswith("DIFF satin-outcome-v2 ") and \
        pinned["diff"] == " ".join(pinned["diff"].split()), pinned["diff"]
    assert pinned["readset"].startswith("READSET satin-readset-v1 "), pinned["readset"]
    rs_total = pinned["readset"].split()[2]
    assert rs_total in pinned["diff"], "the DIFF line must carry the read-set fingerprint"
    assert "satin-readset-v1" in pinned["diff"], pinned["diff"]

    # Inner gate: the whole package's tests, not the library's alone; locked like CI.
    assert sp.test[:2] == ["cargo", "test"] and "--lib" not in sp.test, sp.test
    assert "-p" in sp.test and "mega-evm" in sp.test and "--locked" in sp.test, sp.test
    # Full gate: the workspace, release, locked, warnings denied (CI's RUSTFLAGS).
    full = raw["correctness_oracle"]["test_full"]
    assert full[:2] == ["env", "RUSTFLAGS=-D warnings"], full
    for tok in ("--workspace", "--release", "--locked"):
        assert tok in full, (tok, full)

    # Editable: the four directories the campaign narrows to, never the instruments' code
    # (test-utils modules), and never a file: the scope is a subtree, not a list of files.
    assert sp.regions == [
        "crates/mega-evm/src/access",
        "crates/mega-evm/src/evm",
        "crates/mega-evm/src/limit",
        "crates/mega-evm/src/system",
    ], sp.regions
    for region in sp.regions:
        assert region.startswith("crates/mega-evm/src/"), region
        assert "test_utils" not in region and "hasher" not in region, region
    assert sp.constraints["no_new_deps"] and sp.constraints["byte_identical"]

    # Certification lanes: the op-revm arms are the controls; realistic families protected.
    assert sp.control_lanes == ["op_revm"], sp.control_lanes
    assert set(raw["protected_row_families"]) == {"block", "corpus"}
    assert set(sp.terminal_bench_targets) == {"transact", "corpus", "block", "factory"}
    assert raw["pinned_tools"]["rustc"] == "1.97.0-nightly"

    # Terminal = the bench lane on Satin's own CodSpeed benches; no probe lane; CodSpeed's
    # profile rules; the confirm layer on.
    assert sp.terminal_lane == "bench" and raw.get("terminal_lane") == "bench"
    assert sp.profile_fidelity == "codspeed-ci"
    assert "terminal_probe_workloads" not in raw and "terminal_probe_scales" not in raw
    assert sp.codspeed_confirm.get("enabled") is True, sp.codspeed_confirm
    assert sp.codspeed_confirm["rounds"] >= 1

    # The probe's rows are the CodSpeed subject rows (captured at the baseline): every one is
    # spelled in the probe, so a probe edit that drops a row is caught here; the full check is
    # probes/codspeed_rowmap.py against a checkout.
    rows = [l.strip() for l in (ROOT / "targets/mega-evm-satin.codspeed-rows.txt").read_text()
            .splitlines() if l.strip() and not l.startswith("#")]
    subject = [r for r in rows if "op_revm" not in r.split("/")]
    assert len(rows) == 57 and len(subject) == 41, (len(rows), len(subject))
    assert "block/erc20_transfers" in subject, subject
    src = (ROOT / sp.bench["probe"]).read_text()
    assert "fn aro_probe_measured(" in src and "#[no_mangle]" in src
    assert "ARO_PROBE_LIST" in src and "ARO_PROBE_ROW" in src
    for row in subject:
        family, rest = row.split("/", 1)
        if family == "transact":
            workload, arm = rest.split("/")
            assert (f'"{workload}"' in src and arm == "satin") or f'"{rest}"' in src, row
        elif family == "corpus":
            assert f'"{rest}"' in src, row
        else:
            assert f'"{row}"' in src, row
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
witness = "--json-witness" in args
paths = [a for a in args if a.startswith("/")]
files = sorted(p for d in paths for p in Path(d).rglob("*.json"))
lines = []
for f in files:
    seed = f"{{mode}}{{tight}}{{f.name}}" + ("read" if witness else "")
    row = {{"mode": mode, "path": str(f), "test": f.stem, "d": 0, "g": 0, "v": 0,
           "digest": "0x" + hashlib.sha256(seed.encode()).hexdigest()[:8],
           "row": "read" if witness else "outcome"}}
    if witness and (checkout / "mutate-witness").exists() and f.name == "b.json":
        row["digest"] = "0xbeef"
    if not witness and (checkout / "mutate").exists() and mode == "satin" and f.name == "b.json":
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
        "}\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
        "    #[test]\n    fn hot_adds_one() {\n        assert_eq!(hot(1), 2);\n    }\n}\n")
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
        assert fp.startswith("DIFF satin-outcome-v2 "), fp
        # The read set of the same runs is its own fingerprint, and the DIFF line carries both.
        assert "satin-readset-v1" in fp, fp
        readset = [l for l in out.stdout.splitlines() if l.startswith("READSET ")]
        assert len(readset) == 1 and readset[0].startswith("READSET satin-readset-v1 "), readset
        assert [l for l in out.stdout.splitlines() if l.startswith("RUNREAD ")][:1], out.stdout
        for part in ("equivalence-Osaka=3:0:", "tight-Amsterdam=3:0:", "btest-Osaka=3:0:",
                     "cfg-fence=5:0:"):
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
        # ... and so does an edit to the unit tests (cfg(test)): weakening an assertion, or a
        # stray #[test] added outside the test module. The module's own #[test] is one item
        # with it, not two.
        for edited in (text.replace("s.a = None;", "s.a = Some(0);"),
                       text.replace("let neutral = false;", "let neutral = true;"),
                       text.replace("s.a = s.b;", "s.a = s.b.take();"),
                       text + '#[cfg(feature = "test-utils")]\npub fn fast() {}\n',
                       text.replace("assert_eq!(hot(1), 2);", "assert!(hot(1) > 0);"),
                       text + "#[test]\nfn stray() {}\n"):
            evm.write_text(edited)
            rc, fp_f, _ = _drive(cand, fx_a, fake)
            assert rc == 0 and fp_f != fp, edited
            assert [t for t in fp_f.split() if t.startswith("cfg-fence=")] != \
                [t for t in fp.split() if t.startswith("cfg-fence=")]
        evm.write_text(text)

        # Pinned line counts: the right corpus passes, a short one is exit 2 before any DIFF.
        pins = ("equivalence-Osaka=3,equivalence-Amsterdam=3,satin-Osaka=3,satin-Amsterdam=3,"
                "tight-Osaka=3,tight-Amsterdam=3,btest-Osaka=3")
        rc, fp_p, _ = _drive(base, fx_a, fake, "--expect-lines", pins)
        assert rc == 0 and fp_p == fp, (rc, fp_p)
        short = _fixtures(tmp / "fx-short")
        (short / "devnet/state_tests/c.json").unlink()
        rc, fp_s, out = _drive(base, short, fake, "--expect-lines", pins)
        assert rc == 2 and fp_s is None, (rc, out.stdout)
        assert "equivalence-Amsterdam printed 2 lines, the pinned corpus has 3" in out.stderr
        rc, _, out = _drive(base, short, fake)     # unpinned, the short corpus would "pass"
        assert rc == 0
        rc, _, out = _drive(base, fx_a, fake, "--expect-lines", "nonsense-run=3")
        assert rc == 2 and "not RUN=N" in out.stderr

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
        # Attempt 2's agent reused the id `c1` (ids are per attempt): frozen there.
        p3 = Patch([Edit("src/hot.rs", "fn hot()", "fn hot_fast()")])
        (run / "a2/patches").mkdir(parents=True)
        (run / "a2/patches/c1.txt").write_text(patchfile.dump(p3))
        entry = {"attempt": "a1", "fn": "hot", "files": ["src/hot.rs"], "regime":
                 "byte-identical", "critic_verdict": "pass", "mergeable": False}
        (run / "manifest.json").write_text(json.dumps({
            "spec": "mega-evm-satin", "baseline_ref": base, "run_id": "r1", "accepted": [
                {**entry, "order": 2, "id": "c2", "delta_pct": -0.8,
                 "patch_path": "a1/patches/c2.txt", "hypothesis": "inline hot"},
                {**entry, "order": 1, "id": "c1", "delta_pct": -1.5,
                 "patch_path": "a1/patches/c1.txt", "hypothesis": "cheaper string",
                 "codspeed_confirm": {"required": True, "status": "confirmed"}}],
            "frozen": [{"attempt": "a2", "id": "c1", "fn": "cold",
                        "patch_path": "a2/patches/c1.txt",
                        "inner_verdict": "accepted-ir", "status": "disagreed",
                        "claimed": ["transact/a/satin"],
                        "reasons": ["claimed row transact/a/satin did not improve"]}]}))
        (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {"event": "gate", "attempt": 1, "candidate": "c1", "gate": "differential",
             "status": "ok", "fingerprint": "DIFF satin-outcome-v1 abc"},
            {"event": "gate", "attempt": 1, "candidate": "c2", "gate": "test", "status": "ok"},
            {"event": "gate", "attempt": 1, "candidate": "zz", "gate": "test", "status": "fail"},
            {"event": "gate", "attempt": 2, "candidate": "c1", "gate": "differential",
             "status": "ok", "fingerprint": "DIFF satin-outcome-v1 other-attempt"},
            {"event": "codspeed_confirm", "attempt": 2, "candidate": "c1", "status": "disagreed",
             "rows": {"transact/a/satin": {"probe_base": 1000, "probe_cand": 980,
                                           "codspeed_base": 2000, "codspeed_cand": 2004}}}]))

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
            assert '+    "b \\"quoted\\" str"' in read("entries/01-a1-c1.patch")
            assert "+#[inline]" in read("entries/02-a1-c2.patch")
            assert read("stack.patch").count("Subject: [PATCH") == 2
            # Evidence is per (attempt, id): a1's c1 never picks up a2's c1 events.
            ev1 = read("evidence/01-a1-c1.jsonl")
            assert "satin-outcome-v1 abc" in ev1 and "other-attempt" not in ev1, ev1
            assert "codspeed_cand" not in ev1
            assert "zz" not in read("evidence/02-a1-c2.jsonl")
            summary = read("SUMMARY.txt")
            assert "#1 a1/c1" in summary and "differential=DIFF satin-outcome-v1 abc" in summary
            assert "#2 a1/c2" in summary and "differential=NO GATE EVENT" in summary
            assert "confirm=confirmed" in summary and "frozen=1" in summary
            assert "FROZEN a2/c1" in summary and "confirm=disagreed" in summary
            assert read("frozen/a2-c1.txt") == patchfile.dump(p3)
            fz = read("frozen/a2-c1.jsonl")
            assert '"codspeed_cand": 2004' in fz and "satin-outcome-v1 abc" not in fz, fz
            assert read("stack.patch").count("Subject: [PATCH") == 2, "frozen never stacks"
            assert read("a1/patches/c1.txt".join(["patches/", ""])) == patchfile.dump(p1)
        wts = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True,
                             text=True).stdout.strip().splitlines()
        assert len(wts) == 1, wts
    print("case_76 OK: hand-back bundle round-trips with patches and evidence")


def case_84():
    """Satin target: a read-set-only change is rejected, its outcome digest unchanged."""
    print("=== case 84: the read-set differential rejects a reads-only change ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fake = tmp / "state-test"
        fake.write_text(_FAKE.format(python=sys.executable))
        fake.chmod(0o755)
        fx = _fixtures(tmp / "fx")
        base, cand = _checkout(tmp / "base"), _checkout(tmp / "cand")

        rc, fp_base, out = _drive(base, fx, fake)
        assert rc == 0 and fp_base, (rc, out.stdout, out.stderr)
        rc, fp_same, _ = _drive(cand, fx, fake)
        assert rc == 0 and fp_same == fp_base, "an identical candidate passes"

        # The candidate reads differently and its outcome is untouched: only the read set moves.
        (cand / "mutate-witness").write_text("")
        rc, fp_reads, _ = _drive(cand, fx, fake)
        assert rc == 0 and fp_reads, (rc, fp_reads)
        assert fp_reads != fp_base, "a read-set change must move the DIFF line ARO compares"
        # Nothing but the two read-set fingerprints moved: the same runs, the same outcomes.
        outcome = lambda line: [t for t in line.split() if "=" in t][:8]  # the 7 runs + fence
        assert outcome(fp_reads) == outcome(fp_base), (outcome(fp_reads), outcome(fp_base))
        readset = lambda line: [t for t in line.split() if "=" in t][8:]
        assert readset(fp_reads) != readset(fp_base), (readset(fp_reads), readset(fp_base))
        assert len(readset(fp_reads)) == len(readset(fp_base)) == 7, readset(fp_reads)
        # The same comparison the gate runs: the two fingerprints differ, so the candidate fails.
        assert fp_reads != fp_base
        (cand / "mutate-witness").unlink()
    print("case_84 OK: a reads-only change is rejected by the read-set differential")


def case_85():
    """Satin target: an identical read set passes, and the read set covers the block tests."""
    print("=== case 85: the read-set differential passes when the reads are identical ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fake = tmp / "state-test"
        fake.write_text(_FAKE.format(python=sys.executable))
        fake.chmod(0o755)
        fx = _fixtures(tmp / "fx")
        base, cand = _checkout(tmp / "base"), _checkout(tmp / "cand")
        # The fake records which runs it was asked for, so the read-set pass can be inspected.
        rc, fp_a, out_a = _drive(base, fx, fake)
        assert rc == 0 and fp_a
        asked = [l for l in out_a.stdout.splitlines() if l.startswith("RUNREAD ")]
        assert len(asked) == 7, asked     # the six state-test runs and the block tests
        assert any(a.startswith("RUNREAD btest-Osaka ") for a in asked), asked

        rc, fp_b, _ = _drive(cand, fx, fake)
        assert rc == 0 and fp_b == fp_a, "identical checkouts fingerprint identically"
        # A read set that loses a run is not vouched for: no fingerprint at all.
        (cand / "silent").write_text("")
        rc, fp_s, out = _drive(cand, fx, fake)
        assert rc == 1 and fp_s is None and "read set printed no line" in out.stderr, \
            (rc, out.stderr)
    print("case_85 OK: an identical read set passes and a lost one is refused")
