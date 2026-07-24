#!/usr/bin/env python3
import json
import re
from pathlib import Path

root = Path("/nvme2/mega-engineer/workspace/aro/docs/data/mega-evm-ckpt-overshoot-20260724")
paths = list(root.rglob("*")) + [
    Path("/nvme2/mega-engineer/workspace/aro/docs/mega-evm-ckpt-overshoot-distribution-20260724.md"),
    Path("/nvme2/mega-engineer/workspace/aro/docs/data/sample_mainnet_overshoot.py"),
]
hits = []
n = 0
for p in paths:
    if not p.is_file() or p.stat().st_size > 100_000_000:
        continue
    n += 1
    t = p.read_text(errors="ignore")
    if "d332c06c4dd056fd31f8466cf503756d2dd36a9b" in t:
        hits.append({"file": str(p), "kind": "key_hex"})
    if "quiknode.pro/" in t.lower():
        hits.append({"file": str(p), "kind": "quiknode_path"})
    if "ghp_" in t:
        hits.append({"file": str(p), "kind": "ghp"})
    for m in re.finditer(r"https://\S+", t):
        s = m.group(0)
        if "quiknode" in s.lower() or "d332c06c" in s.lower():
            hits.append({"file": str(p), "kind": "url"})

rep = {
    "scanned_files": n,
    "hits": len(hits),
    "hit_details": hits,
    "clean": len(hits) == 0,
}
(root / "mainnet/secret_scan.json").write_text(json.dumps(rep, indent=2) + "\n")
print(json.dumps(rep, indent=2))

main = json.loads((root / "mainnet/mainnet_analysis.json").read_text())
prev = json.loads((root / "analysis/analysis.json").read_text())
merged = {
    "synthetic_decision_table": prev["decision_table"],
    "mainnet_decision_rows": main["decision_rows_mainnet"],
    "mainnet_window": main["window"],
    "mainnet_answers": main["answers"],
    "mainnet_stability": main["stability"],
    "secret_scan": rep,
}
(root / "merged_decision.json").write_text(json.dumps(merged, indent=2) + "\n")
print("merged_ok")
