#!/usr/bin/env python3
"""Pack a finished Satin campaign for hand-back: one base64 text the supervisor decodes.

    satin_handback.py RUN_DIR --repo MEGA_EVM_CHECKOUT [--out PREFIX]

Reads RUN_DIR/manifest.json (`python3 -m aro manifest RUN_DIR --spec ...` writes it), applies the
accepted entries in `order` on a throwaway worktree of the checkout at the manifest's baseline,
committing each, and packs into PREFIX.tgz:

- manifest.json, events.jsonl and, when present, REPORT.md, reverify.json, terminal.json;
- the raw ARO patches (`aN/patches/<id>.txt`, SEARCH/REPLACE blocks);
- `entries/<order>-<id>.patch`: each entry as a `git format-patch` mail (applies with `git am`),
  and `stack.patch`, all of them in order;
- `evidence/<order>-<id>.jsonl`: every event of events.jsonl that names the candidate (its gates,
  the differential fingerprint it matched, Ir, significance, critic, the CodSpeed confirm, the
  baseline advance);
- `frozen/<id>.txt` and `frozen/<id>.jsonl`: each candidate the inner loop accepted but the
  CodSpeed confirm did not (manifest `frozen`): its raw patch and its events, the
  `codspeed_confirm` event with both sides' counts per row among them;
- SUMMARY.txt: one line per entry and per frozen candidate.

Then writes PREFIX.tgz.b64 (base64, the form to send: a plain patch pasted into a message has had
its quotes rewritten before) and prints the tgz's sha256. Decode with
`base64 -d < PREFIX.tgz.b64 > PREFIX.tgz && shasum -a 256 PREFIX.tgz && tar xzf PREFIX.tgz`.
The worktree is removed afterwards; nothing is pushed or committed in the checkout's branches.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aro import patchfile  # noqa: E402  (the aro repo this probe lives in)


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=check)


def names(row: dict, cid: str) -> bool:
    return row.get("candidate") == cid or row.get("id") == cid or row.get("cid") == cid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--repo", type=Path, required=True, help="the mega-evm checkout")
    ap.add_argument("--out", type=Path, help="output prefix (default RUN_DIR-handback)")
    args = ap.parse_args()
    run = args.run_dir.resolve()
    manifest_path = run / "manifest.json"
    if not manifest_path.exists():
        print(f"no {manifest_path}: run `python3 -m aro manifest {run} --spec ...` first",
              file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    baseline = manifest.get("baseline_ref")
    entries = sorted(manifest.get("accepted") or [], key=lambda e: e.get("order") or 0)
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()
              if line.strip()] if (run / "events.jsonl").exists() else []
    prefix = (args.out or run.parent / f"{run.name}-handback").resolve()

    stage = Path(tempfile.mkdtemp(prefix="satin-handback-"))
    pack = stage / prefix.name
    (pack / "entries").mkdir(parents=True)
    (pack / "evidence").mkdir()
    wt = stage / "wt"
    git(args.repo, "worktree", "add", "--detach", str(wt), baseline)
    try:
        summary = []
        for e in entries:
            order, cid = e.get("order"), e.get("id")
            tag = f"{order:02d}-{patchfile.safe_id(cid)}"
            patch_file = run / e["patch_path"]
            raw_dest = pack / "patches" / e["patch_path"]
            raw_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(patch_file, raw_dest)
            for edit in patchfile.parse(patch_file.read_text()):
                f = wt / edit.path
                text = f.read_text()
                if text.count(edit.search) != 1:
                    raise SystemExit(f"entry {order} ({cid}): search text not unique in {edit.path}")
                f.write_text(text.replace(edit.search, edit.replace, 1))
            subject = (e.get("hypothesis") or "").strip().splitlines()[:1] or [""]
            git(wt, "-c", "user.name=aro", "-c", "user.email=aro@localhost", "commit", "-q",
                "-a", "-m", f"aro {manifest.get('spec')} #{order} {cid} {e.get('fn')}: "
                f"{subject[0][:120]}", "-m",
                f"delta_pct={e.get('delta_pct')} regime={e.get('regime')} "
                f"critic={e.get('critic_verdict')} mergeable={e.get('mergeable')}")
            (pack / "entries" / f"{tag}.patch").write_text(
                git(wt, "format-patch", "-1", "HEAD", "--stdout").stdout)
            mine = [row for row in events if names(row, cid)]
            (pack / "evidence" / f"{tag}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in mine))
            fps = [row.get("fingerprint") for row in mine
                   if row.get("event") == "gate" and row.get("gate") == "differential"]
            rev = (e.get("reverify") or {}).get("verdict")
            summary.append(
                f"#{order} {cid} fn={e.get('fn')} files={','.join(e.get('files') or [])} "
                f"delta_pct={e.get('delta_pct')} regime={e.get('regime')} "
                f"critic={e.get('critic_verdict')} mergeable={e.get('mergeable')} "
                f"reverify={rev} terminal={e.get('terminal')} "
                f"confirm={(e.get('codspeed_confirm') or {}).get('status')} "
                f"differential={(fps[-1] or 'ok') if fps else 'NO GATE EVENT'}")
        for f in manifest.get("frozen") or []:
            cid = f.get("id")
            tag = patchfile.safe_id(cid)
            (pack / "frozen").mkdir(exist_ok=True)
            if f.get("patch_path") and (run / f["patch_path"]).exists():
                shutil.copy2(run / f["patch_path"], pack / "frozen" / f"{tag}.txt")
            mine = [row for row in events if names(row, cid)]
            (pack / "frozen" / f"{tag}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in mine))
            summary.append(
                f"FROZEN {cid} fn={f.get('fn')} inner={f.get('inner_verdict')} "
                f"confirm={f.get('status')} claimed={','.join(f.get('claimed') or [])} "
                f"reasons={' | '.join(f.get('reasons') or [])}")
        if entries:
            (pack / "stack.patch").write_text(
                git(wt, "format-patch", "--stdout", f"{baseline}..HEAD").stdout)
        (pack / "SUMMARY.txt").write_text(
            f"spec={manifest.get('spec')} baseline={baseline} run_id={manifest.get('run_id')} "
            f"accepted={len(entries)} frozen={len(manifest.get('frozen') or [])}\n"
            + "".join(line + "\n" for line in summary))
        for name in ("manifest.json", "events.jsonl", "REPORT.md", "reverify.json",
                     "terminal.json"):
            if (run / name).exists():
                shutil.copy2(run / name, pack / name)
    finally:
        git(args.repo, "worktree", "remove", "--force", str(wt), check=False)
        git(args.repo, "worktree", "prune", check=False)

    tgz = prefix.with_name(prefix.name + ".tgz")
    with tarfile.open(tgz, "w:gz") as tar:
        tar.add(pack, arcname=prefix.name)
    shutil.rmtree(stage, ignore_errors=True)
    b64 = prefix.with_name(prefix.name + ".tgz.b64")
    b64.write_bytes(base64.encodebytes(tgz.read_bytes()))
    sha = hashlib.sha256(tgz.read_bytes()).hexdigest()
    print(f"{len(entries)} accepted entries packed")
    print(f"bundle: {tgz}\nsend:   {b64} ({b64.stat().st_size} bytes)\nsha256: {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
