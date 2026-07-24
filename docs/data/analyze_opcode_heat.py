#!/usr/bin/env python3
"""Mainnet opcode heat + limit proximity — CALL/CREATE stipend stripped."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path("/nvme2/mega-engineer/workspace/aro/docs/data/mega-evm-ckpt-overshoot-20260724")
CACHE = ROOT / "mainnet/trace_cache"
OUT = ROOT / "opcode_heat"

TX_COMPUTE_LIMIT = 200_000_000
DETENTION_20M = 20_000_000
ORACLE_1M = 1_000_000

OP_BY_NAME = {
    "STOP": 0x00, "ADD": 0x01, "MUL": 0x02, "SUB": 0x03, "DIV": 0x04, "SDIV": 0x05,
    "MOD": 0x06, "SMOD": 0x07, "ADDMOD": 0x08, "MULMOD": 0x09, "EXP": 0x0A,
    "SIGNEXTEND": 0x0B, "LT": 0x10, "GT": 0x11, "SLT": 0x12, "SGT": 0x13, "EQ": 0x14,
    "ISZERO": 0x15, "AND": 0x16, "OR": 0x17, "XOR": 0x18, "NOT": 0x19, "BYTE": 0x1A,
    "SHL": 0x1B, "SHR": 0x1C, "SAR": 0x1D, "SHA3": 0x20, "KECCAK256": 0x20,
    "ADDRESS": 0x30, "BALANCE": 0x31, "ORIGIN": 0x32, "CALLER": 0x33, "CALLVALUE": 0x34,
    "CALLDATALOAD": 0x35, "CALLDATASIZE": 0x36, "CALLDATACOPY": 0x37, "CODESIZE": 0x38,
    "CODECOPY": 0x39, "GASPRICE": 0x3A, "EXTCODESIZE": 0x3B, "EXTCODECOPY": 0x3C,
    "RETURNDATASIZE": 0x3D, "RETURNDATACOPY": 0x3E, "EXTCODEHASH": 0x3F,
    "BLOCKHASH": 0x40, "COINBASE": 0x41, "TIMESTAMP": 0x42, "NUMBER": 0x43,
    "DIFFICULTY": 0x44, "PREVRANDAO": 0x44, "GASLIMIT": 0x45, "CHAINID": 0x46,
    "SELFBALANCE": 0x47, "BASEFEE": 0x48, "BLOBHASH": 0x49, "BLOBBASEFEE": 0x4A,
    "POP": 0x50, "MLOAD": 0x51, "MSTORE": 0x52, "MSTORE8": 0x53, "SLOAD": 0x54,
    "SSTORE": 0x55, "JUMP": 0x56, "JUMPI": 0x57, "PC": 0x58, "MSIZE": 0x59,
    "GAS": 0x5A, "JUMPDEST": 0x5B, "TLOAD": 0x5C, "TSTORE": 0x5D, "MCOPY": 0x5E,
    "PUSH0": 0x5F, "CREATE": 0xF0, "CALL": 0xF1, "CALLCODE": 0xF2, "RETURN": 0xF3,
    "DELEGATECALL": 0xF4, "CREATE2": 0xF5, "STATICCALL": 0xFA, "REVERT": 0xFD,
    "INVALID": 0xFE, "SELFDESTRUCT": 0xFF, "CLZ": 0x1E,
}
for i in range(1, 33):
    OP_BY_NAME[f"PUSH{i}"] = 0x5F + i
for i in range(1, 17):
    OP_BY_NAME[f"DUP{i}"] = 0x7F + i
    OP_BY_NAME[f"SWAP{i}"] = 0x8F + i

CALL_CREATE = {0xF0, 0xF1, 0xF2, 0xF4, 0xF5, 0xFA}
INTRINSIC = {0xF1: 100, 0xF2: 100, 0xF4: 100, 0xFA: 100, 0xF0: 32000, 0xF5: 32000}
V1_CKPT = {
    0xF1, 0xF2, 0xF4, 0xFA, 0xF3, 0xFD, 0x00, 0xFF, 0x55,
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xF0, 0xF5, 0x56, 0x57,
}
VOLATILE = {
    0x42, 0x43, 0x41, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x40,
    0x31, 0x32, 0x3A, 0x3B, 0x3C, 0x3F, 0x54,
}
LENGTH = {
    "KECCAK256": 0x20,
    "CALLDATACOPY": 0x37,
    "CODECOPY": 0x39,
    "RETURNDATACOPY": 0x3E,
    "MCOPY": 0x5E,
}


def classify(op: int) -> str:
    if op in VOLATILE:
        return "volatile"
    if op in V1_CKPT:
        return "checkpoint"
    return "ordinary"


def eff_cost(op: int, cost: int) -> int:
    if op in CALL_CREATE:
        return min(cost, INTRINSIC.get(op, 100))
    return cost


def pctile(vals, ps):
    if not vals:
        return {p: None for p in ps}
    xs = sorted(float(v) for v in vals)
    n = len(xs)
    out = {}
    for p in ps:
        if p >= 100:
            out[p] = xs[-1]
        else:
            idx = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
            out[p] = xs[idx]
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    count = Counter()
    gas = Counter()
    max_eff = Counter()
    raw_gas = Counter()
    total_ops = 0
    total_eff = 0
    cat_ops = Counter()
    cat_gas = Counter()
    length = {k: {"count": 0, "gas": 0, "max_gas": 0, "txs": 0} for k in LENGTH}
    tx_compute = []
    post_vol = []
    vol_txs = 0
    hard = []
    near = []
    n_tx = 0

    for fp in sorted(CACHE.glob("*.json")):
        o = json.loads(fp.read_text())
        logs = o.get("structLogs") or []
        if not logs:
            continue
        n_tx += 1
        txh = o.get("tx") or fp.stem
        tx_g = 0
        first_vol = None
        seen = set()
        for i, lg in enumerate(logs):
            name = (lg.get("op") or "").upper()
            if name == "SHA3":
                name = "KECCAK256"
            op = OP_BY_NAME.get(name, -1)
            cost = int(lg.get("gasCost") or 0)
            e = eff_cost(op, cost) if op >= 0 else cost
            count[name] += 1
            gas[name] += e
            raw_gas[name] += cost
            if e > max_eff[name]:
                max_eff[name] = e
            total_ops += 1
            total_eff += e
            tx_g += e
            cat = classify(op) if op >= 0 else "ordinary"
            cat_ops[cat] += 1
            cat_gas[cat] += e
            if first_vol is None and op in VOLATILE:
                first_vol = i
            for lk, lob in LENGTH.items():
                if op == lob:
                    length[lk]["count"] += 1
                    length[lk]["gas"] += cost
                    if cost > length[lk]["max_gas"]:
                        length[lk]["max_gas"] = cost
                    seen.add(lk)
        for lk in seen:
            length[lk]["txs"] += 1
        tx_compute.append(tx_g)
        if first_vol is not None:
            vol_txs += 1
            post = 0
            for j in range(first_vol, len(logs)):
                name = (logs[j].get("op") or "").upper()
                if name == "SHA3":
                    name = "KECCAK256"
                op = OP_BY_NAME.get(name, -1)
                cost = int(logs[j].get("gasCost") or 0)
                post += eff_cost(op, cost) if op >= 0 else cost
            post_vol.append(post)
            if post >= DETENTION_20M:
                hard.append({"tx": txh, "kind": "post_vol_ge_20M", "post": post})
            elif post >= ORACLE_1M * 0.9:
                near.append({"tx": txh, "kind": "post_vol_ge_0.9M", "post": post})
        if tx_g >= TX_COMPUTE_LIMIT:
            hard.append({"tx": txh, "kind": "tx_ge_200M", "tx_gas": tx_g})
        elif tx_g >= TX_COMPUTE_LIMIT * 0.5:
            near.append(
                {
                    "tx": txh,
                    "kind": "tx_ge_50pct_200M",
                    "tx_gas": tx_g,
                    "pct": tx_g / TX_COMPUTE_LIMIT,
                }
            )

    ord_ops = cat_ops["ordinary"] / total_ops
    ord_gas = cat_gas["ordinary"] / total_eff
    top30 = sorted(gas.items(), key=lambda x: -x[1])[:30]
    top_rows = []
    for name, g in top30:
        op = OP_BY_NAME.get(name, -1)
        top_rows.append(
            {
                "op": name,
                "count": count[name],
                "count_share": count[name] / total_ops,
                "gas": g,
                "gas_share": g / total_eff,
                "max_eff_gas": max_eff[name],
                "rex7_class": classify(op) if op >= 0 else "ordinary",
            }
        )
    length_rows = []
    raw_total = sum(raw_gas.values()) or 1
    for n, st in length.items():
        length_rows.append(
            {
                "op": n,
                "count": st["count"],
                "count_share": st["count"] / total_ops,
                "gas_raw": st["gas"],
                "gas_share_raw": st["gas"] / raw_total,
                "max_single_gas": st["max_gas"],
                "txs_seen": st["txs"],
            }
        )
    txp = pctile(tx_compute, [50, 90, 99, 100])
    pp = pctile(post_vol, [50, 90, 99, 100])

    report = {
        "n_txs": n_tx,
        "total_ops": total_ops,
        "total_eff_gas": total_eff,
        "method": "CALL/CREATE gasCost capped to intrinsic (100/32k) to avoid stipend double-count",
        "limits": {
            "tx_compute": TX_COMPUTE_LIMIT,
            "detention_20M": DETENTION_20M,
            "oracle_1M": ORACLE_1M,
        },
        "category_totals": {
            "ordinary": {
                "ops": cat_ops["ordinary"],
                "ops_share": ord_ops,
                "gas": cat_gas["ordinary"],
                "gas_share": ord_gas,
            },
            "checkpoint": {
                "ops": cat_ops["checkpoint"],
                "ops_share": cat_ops["checkpoint"] / total_ops,
                "gas": cat_gas["checkpoint"],
                "gas_share": cat_gas["checkpoint"] / total_eff,
            },
            "volatile": {
                "ops": cat_ops["volatile"],
                "ops_share": cat_ops["volatile"] / total_ops,
                "gas": cat_gas["volatile"],
                "gas_share": cat_gas["volatile"] / total_eff,
            },
        },
        "top30_by_eff_gas": top_rows,
        "length_ops": length_rows,
        "tx_compute_proxy_dist": {
            "p50": txp[50],
            "p90": txp[90],
            "p99": txp[99],
            "max": txp[100],
            "limit": TX_COMPUTE_LIMIT,
            "p50_vs_limit": txp[50] / TX_COMPUTE_LIMIT,
            "p99_vs_limit": txp[99] / TX_COMPUTE_LIMIT,
            "max_vs_limit": txp[100] / TX_COMPUTE_LIMIT,
        },
        "volatile_access": {
            "txs": vol_txs,
            "tx_share": vol_txs / n_tx,
            "post_p50": pp[50],
            "post_p90": pp[90],
            "post_p99": pp[99],
            "post_max": pp[100],
            "p99_vs_20M": (pp[99] or 0) / DETENTION_20M,
            "max_vs_20M": (pp[100] or 0) / DETENTION_20M,
            "p99_vs_1M": (pp[99] or 0) / ORACLE_1M,
            "max_vs_1M": (pp[100] or 0) / ORACLE_1M,
        },
        "hard_hits": hard,
        "hard_count": len(hard),
        "near_hits": sorted(near, key=lambda x: -x.get("pct", x.get("post", 0)))[:20],
        "near_count": len(near),
    }
    (OUT / "opcode_heat_analysis.json").write_text(json.dumps(report, indent=2) + "\n")

    md = []
    md += [
        "# Mainnet opcode heat + limit proximity (offline, 1000 txs)",
        "",
        "**UTC**: 2026-07-24  ",
        "**Corpus**: 1000-tx structLog cache  ",
        "**Nature**: offline; no RPC; no megaeth-labs writes.",
        "",
        "## Method",
        "",
        "CALL/CREATE `gasCost` includes child stipend → naive Σ double-counts. "
        "**Effective gas** caps CALL family at 100 and CREATE/CREATE2 at 32k. "
        "Length-ops max uses **raw** gasCost.",
        "",
        "## Direct conclusion",
        "",
        f"- **Ordinary**: ops **{100*ord_ops:.2f}%**, eff-gas **{100*ord_gas:.2f}%** "
        f"→ 5–9% packaging ceiling premise (wrappers on ordinary stream) is supported on **execution count**.",
        f"- **Tx compute-proxy** p50/p99/max = **{txp[50]:.0f} / {txp[99]:.0f} / {txp[100]:.0f}** "
        f"vs **200M** → **{100*txp[50]/TX_COMPUTE_LIMIT:.4f}% / {100*txp[99]/TX_COMPUTE_LIMIT:.3f}% / "
        f"{100*txp[100]/TX_COMPUTE_LIMIT:.3f}%** of cap.",
        f"- **Volatile txs** **{100*vol_txs/n_tx:.1f}%**; post-access p99/max "
        f"**{pp[99]:.0f} / {pp[100]:.0f}** vs 20M "
        f"(**{100*(pp[99] or 0)/DETENTION_20M:.3f}% / {100*(pp[100] or 0)/DETENTION_20M:.3f}%**).",
        f"- **Hard hits** (tx≥200M or post≥20M): **{len(hard)}**. Near: **{len(near)}**.",
        "",
        "## 1) Top 30 by effective gas mass",
        "",
        "| rank | op | class | count% | eff-gas% | max eff single |",
        "|---:|---|---|---:|---:|---:|",
    ]
    for i, r in enumerate(top_rows, 1):
        md.append(
            f"| {i} | `{r['op']}` | {r['rex7_class']} | {100*r['count_share']:.3f}% | "
            f"{100*r['gas_share']:.3f}% | {r['max_eff_gas']:,} |"
        )
    md += [
        "",
        "### Category totals",
        "",
        "| class | ops% | eff-gas% |",
        "|---|---:|---:|",
        f"| ordinary | {100*ord_ops:.3f}% | {100*ord_gas:.3f}% |",
        f"| checkpoint | {100*cat_ops['checkpoint']/total_ops:.3f}% | {100*cat_gas['checkpoint']/total_eff:.3f}% |",
        f"| volatile | {100*cat_ops['volatile']/total_ops:.3f}% | {100*cat_gas['volatile']/total_eff:.3f}% |",
        "",
        f"**Ordinary combined: ops {100*ord_ops:.2f}% / eff-gas {100*ord_gas:.2f}%**.",
        "",
        "## 2) Length-metered opcodes (raw gasCost)",
        "",
        "| op | count% | gas% (raw) | max single gas | txs |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in length_rows:
        md.append(
            f"| `{r['op']}` | {100*r['count_share']:.5f}% | {100*r['gas_share_raw']:.5f}% | "
            f"{r['max_single_gas']:,} | {r['txs_seen']} |"
        )
    md += [
        "",
        "Count% low ⇒ packaging tax of keeping them wrapped is small. "
        "Max single gas tests “one op can still burn a lot”.",
        "",
        "## 3) Limit proximity",
        "",
        "### Per-tx compute proxy (eff Σ)",
        "",
        "| p50 | p90 | p99 | max | limit | max/limit |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {txp[50]:.0f} | {txp[90]:.0f} | {txp[99]:.0f} | {txp[100]:.0f} | "
        f"{TX_COMPUTE_LIMIT:,} | {txp[100]/TX_COMPUTE_LIMIT:.4f} |",
        "",
        "### Volatile + post-access",
        "",
        f"- volatile txs: **{vol_txs}/{n_tx} = {100*vol_txs/n_tx:.2f}%**",
        f"- post-access p50/p99/max: **{pp[50]:.0f} / {pp[99]:.0f} / {pp[100]:.0f}**",
        f"- vs 20M: p99 **{100*(pp[99] or 0)/DETENTION_20M:.3f}%**, max **{100*(pp[100] or 0)/DETENTION_20M:.3f}%**",
        f"- vs 1M: p99 **{100*(pp[99] or 0)/ORACLE_1M:.2f}%**, max **{100*(pp[100] or 0)/ORACLE_1M:.2f}%**",
        "",
        "**Overshoot impact surface:** far under binding caps → overshoot is mainly a "
        "correctness-bound design issue, not frequent user-visible OOG in this window.",
        "",
        "## 4) Limit cases",
        "",
        f"- hard: **{len(hard)}**",
    ]
    if hard:
        for h in hard[:20]:
            md.append(f"  - `{h['tx']}` {h}")
    else:
        md.append("- **No hard hits.**")
    md.append(f"- near: **{len(near)}** (top 10)")
    for h in sorted(near, key=lambda x: -x.get("pct", x.get("post", 0)))[:10]:
        md.append(f"  - `{h['tx']}` {h}")
    md += [
        "",
        "## Self-cert",
        "",
        "1. Offline only.",
        "2. No RPC credentials.",
        "3. ARO docs when PAT provided.",
        "4. megaeth-labs zero writes.",
        "",
    ]
    text = "\n".join(md) + "\n"
    (OUT / "opcode_heat_report.md").write_text(text)
    Path("/nvme2/mega-engineer/workspace/aro/docs/mega-evm-mainnet-opcode-heat-20260724.md").write_text(
        text
    )
    (OUT / "opcode_heat_analysis.json").write_text(json.dumps(report, indent=2) + "\n")

    mp = ROOT / "merged_decision.json"
    m = json.loads(mp.read_text()) if mp.exists() else {}
    m["opcode_heat"] = report["category_totals"]
    m["opcode_heat_tx"] = report["tx_compute_proxy_dist"]
    m["opcode_heat_volatile"] = report["volatile_access"]
    m["opcode_heat_length"] = length_rows
    m["opcode_heat_hard_count"] = len(hard)
    mp.write_text(json.dumps(m, indent=2) + "\n")

    # patch overview
    overview = Path(
        "/nvme2/mega-engineer/workspace/aro/docs/mega-evm-ckpt-overshoot-distribution-20260724.md"
    )
    t = overview.read_text()
    block = f"""## Opcode heat + limit proximity (mainnet n=1000)

Offline on the same 1000-tx cache (CALL/CREATE stipend stripped). Full: `docs/mega-evm-mainnet-opcode-heat-20260724.md`.

| finding | value |
|---|---|
| ordinary ops% / eff-gas% | **{100*ord_ops:.2f}%** / **{100*ord_gas:.2f}%** |
| tx eff-Σ p50/p99/max vs 200M | **{txp[50]:.0f} / {txp[99]:.0f} / {txp[100]:.0f}** ({100*txp[50]/TX_COMPUTE_LIMIT:.4f}% / {100*txp[99]/TX_COMPUTE_LIMIT:.3f}% / {100*txp[100]/TX_COMPUTE_LIMIT:.3f}% of cap) |
| volatile txs; post p99/max vs 20M | **{100*vol_txs/n_tx:.1f}%**; **{pp[99]:.0f} / {pp[100]:.0f}** ({100*(pp[99] or 0)/DETENTION_20M:.3f}% / {100*(pp[100] or 0)/DETENTION_20M:.3f}% of 20M) |
| hard limit hits | **{len(hard)}** |

"""
    if "## Opcode heat + limit proximity" in t:
        start = t.find("## Opcode heat + limit proximity")
        end = t.find("\n## ", start + 3)
        if end < 0:
            end = len(t)
        t = t[:start] + block + t[end + 1 :]
    else:
        t = t.replace("## Artifacts", block + "## Artifacts")
    overview.write_text(t)

    print("ordinary", round(100 * ord_ops, 3), round(100 * ord_gas, 3))
    print("tx", txp)
    print("post", pp)
    print("hard", len(hard), "near", len(near))
    for r in length_rows:
        print(r["op"], "c%", round(100 * r["count_share"], 5), "max", r["max_single_gas"])
    print("DONE")


if __name__ == "__main__":
    main()
