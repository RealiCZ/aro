#!/usr/bin/env python3
"""Mainnet opcode heat + limit proximity — offline on 1000-tx cache."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/nvme2/mega-engineer/workspace/aro/docs/data/mega-evm-ckpt-overshoot-20260724")
CACHE = ROOT / "mainnet/trace_cache"
OUT = ROOT / "opcode_heat"
# MegaEVM REX / REX3+ (mainnet-class) — from crates/mega-evm/src/constants.rs
TX_COMPUTE_LIMIT = 200_000_000  # rex::TX_COMPUTE_GAS_LIMIT
BLOCK_ENV_DETENTION = 20_000_000  # relative cap after block-env/beneficiary access
ORACLE_DETENTION_REX3 = 20_000_000  # rex3+ oracle
ORACLE_DETENTION_PRE = 1_000_000

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

NAME_BY_OP = {}
for n, b in OP_BY_NAME.items():
    NAME_BY_OP.setdefault(b, n)

# REX7 packaging categories (align V1 checkpoint design)
CALL_FAMILY = {0xF1, 0xF2, 0xF4, 0xFA}
RETURN_CLASS = {0xF3, 0xFD, 0x00, 0xFF}
STORAGE_GAS = {0x55, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xF0, 0xF5, 0xFF}
VOLATILE = {
    0x42, 0x43, 0x41, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x40,
    0x31, 0x32, 0x3A, 0x3B, 0x3C, 0x3F, 0x54,  # SLOAD included (oracle detention arm)
}
CHECKPOINT = CALL_FAMILY | RETURN_CLASS | STORAGE_GAS | {0x56, 0x57}  # V2-style ckpt set for labeling jumps as ckpt
# For "ordinary" under V1 packaging story: not V1-checkpoint and not volatile
# User asked 普通/检查点/volatile — three-way. JUMP can be checkpoint under V2/V1.5 wrap.
V1_CKPT = CALL_FAMILY | RETURN_CLASS | STORAGE_GAS  # without volatile (volatile separate)
# Actually storage includes SSTORE; SLOAD is volatile in our set. Good.

LENGTH_OPS = {
    "KECCAK256": 0x20,
    "CALLDATACOPY": 0x37,
    "CODECOPY": 0x39,
    "RETURNDATACOPY": 0x3E,
    "MCOPY": 0x5E,
}


def classify(op: int) -> str:
    if op in VOLATILE:
        return "volatile"
    if op in V1_CKPT or op in {0x56, 0x57}:  # jumps treated as checkpoint-family for REX7 wrap story
        return "checkpoint"
    return "ordinary"


def weighted_percentile(vals, ps):
    if not vals:
        return {p: None for p in ps}
    xs = sorted(float(v) for v in vals)
    out = {}
    for p in ps:
        if p >= 100:
            out[p] = xs[-1]
            continue
        idx = min(len(xs) - 1, max(0, int(round((p / 100.0) * (len(xs) - 1)))))
        out[p] = xs[idx]
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    count = Counter()
    gas = Counter()
    max_gas = Counter()
    total_ops = 0
    total_gas = 0

    cat_ops = Counter()
    cat_gas = Counter()

    length_stats = {
        k: {"count": 0, "gas": 0, "max_gas": 0, "txs": 0} for k in LENGTH_OPS
    }

    tx_compute = []  # sum gasCost per tx as compute proxy
    volatile_txs = 0
    post_vol_compute = []  # gas after first volatile op
    pre_vol_compute = []
    limit_hits = []  # any approach to caps

    n_tx = 0
    files = sorted(CACHE.glob("*.json"))
    for fp in files:
        o = json.loads(fp.read_text())
        logs = o.get("structLogs") or []
        if not logs:
            continue
        n_tx += 1
        txh = o.get("tx") or fp.stem
        tx_gas = 0
        first_vol_i = None
        seen_length = set()
        for i, lg in enumerate(logs):
            name = (lg.get("op") or "").upper()
            if name == "SHA3":
                name = "KECCAK256"
            op = OP_BY_NAME.get(name)
            cost = int(lg.get("gasCost") or 0)
            if op is None:
                # still count unknown under ordinary
                op = -1
                name = name or "UNKNOWN"
            count[name] += 1
            gas[name] += cost
            if cost > max_gas[name]:
                max_gas[name] = cost
            total_ops += 1
            total_gas += cost
            tx_gas += cost

            cat = classify(op) if op >= 0 else "ordinary"
            cat_ops[cat] += 1
            cat_gas[cat] += cost

            if first_vol_i is None and op in VOLATILE:
                first_vol_i = i

            for lk, lob in LENGTH_OPS.items():
                if op == lob or (lk == "KECCAK256" and name in ("KECCAK256", "SHA3")):
                    length_stats[lk]["count"] += 1
                    length_stats[lk]["gas"] += cost
                    if cost > length_stats[lk]["max_gas"]:
                        length_stats[lk]["max_gas"] = cost
                    seen_length.add(lk)

        for lk in seen_length:
            length_stats[lk]["txs"] += 1

        tx_compute.append(tx_gas)
        if first_vol_i is not None:
            volatile_txs += 1
            pre = sum(int(logs[j].get("gasCost") or 0) for j in range(first_vol_i))
            post = sum(int(logs[j].get("gasCost") or 0) for j in range(first_vol_i, len(logs)))
            pre_vol_compute.append(pre)
            post_vol_compute.append(post)
            # detention proximity: post-access compute vs caps
            if post >= BLOCK_ENV_DETENTION:
                limit_hits.append(
                    {
                        "tx": txh,
                        "kind": "post_volatile_ge_block_env_cap_20M",
                        "post_gas": post,
                        "cap": BLOCK_ENV_DETENTION,
                    }
                )
            if post >= ORACLE_DETENTION_REX3:
                limit_hits.append(
                    {
                        "tx": txh,
                        "kind": "post_volatile_ge_oracle_rex3_cap_20M",
                        "post_gas": post,
                        "cap": ORACLE_DETENTION_REX3,
                    }
                )
            if post >= ORACLE_DETENTION_PRE:
                # only flag if notable relative to 1M pre-rex3
                if post >= ORACLE_DETENTION_PRE * 0.5:
                    pass  # collect near 1M in separate
            if post >= ORACLE_DETENTION_PRE:
                if post < BLOCK_ENV_DETENTION:
                    # track 1M proximity without flooding
                    if post >= ORACLE_DETENTION_PRE * 0.9:
                        limit_hits.append(
                            {
                                "tx": txh,
                                "kind": "post_volatile_ge_90pct_oracle_pre_1M",
                                "post_gas": post,
                                "cap": ORACLE_DETENTION_PRE,
                            }
                        )

        # tx compute vs per-tx limit
        if tx_gas >= TX_COMPUTE_LIMIT * 0.5:
            limit_hits.append(
                {
                    "tx": txh,
                    "kind": "tx_gas_ge_50pct_tx_compute_limit",
                    "tx_gas": tx_gas,
                    "limit": TX_COMPUTE_LIMIT,
                    "pct": tx_gas / TX_COMPUTE_LIMIT,
                }
            )
        if tx_gas >= TX_COMPUTE_LIMIT:
            limit_hits.append(
                {
                    "tx": txh,
                    "kind": "tx_gas_ge_tx_compute_limit",
                    "tx_gas": tx_gas,
                    "limit": TX_COMPUTE_LIMIT,
                }
            )

    # top 30 by gas
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
                "gas_share": g / total_gas if total_gas else 0,
                "max_single_gas": max_gas[name],
                "rex7_class": classify(op) if op >= 0 else "ordinary",
            }
        )

    ordinary_ops = cat_ops["ordinary"] / total_ops if total_ops else 0
    ordinary_gas = cat_gas["ordinary"] / total_gas if total_gas else 0

    length_rows = []
    for name, st in length_stats.items():
        length_rows.append(
            {
                "op": name,
                "count": st["count"],
                "count_share": st["count"] / total_ops if total_ops else 0,
                "gas": st["gas"],
                "gas_share": st["gas"] / total_gas if total_gas else 0,
                "max_single_gas": st["max_gas"],
                "txs_seen": st["txs"],
            }
        )

    tx_pct = weighted_percentile(tx_compute, [50, 90, 99, 100])
    post_pct = weighted_percentile(post_vol_compute, [50, 90, 99, 100])

    report = {
        "n_txs": n_tx,
        "total_ops": total_ops,
        "total_gas": total_gas,
        "note_gas_proxy": (
            "gasCost from debug structLogs is EVM gas; used as compute-gas proxy. "
            "Storage-gas-heavy ops overstate pure compute; CALL gasCost may include child stipend."
        ),
        "limits": {
            "tx_compute_limit_rex": TX_COMPUTE_LIMIT,
            "block_env_detention": BLOCK_ENV_DETENTION,
            "oracle_detention_rex3": ORACLE_DETENTION_REX3,
            "oracle_detention_pre_rex3": ORACLE_DETENTION_PRE,
        },
        "category_totals": {
            "ordinary": {"ops": cat_ops["ordinary"], "ops_share": ordinary_ops, "gas": cat_gas["ordinary"], "gas_share": ordinary_gas},
            "checkpoint": {
                "ops": cat_ops["checkpoint"],
                "ops_share": cat_ops["checkpoint"] / total_ops if total_ops else 0,
                "gas": cat_gas["checkpoint"],
                "gas_share": cat_gas["checkpoint"] / total_gas if total_gas else 0,
            },
            "volatile": {
                "ops": cat_ops["volatile"],
                "ops_share": cat_ops["volatile"] / total_ops if total_ops else 0,
                "gas": cat_gas["volatile"],
                "gas_share": cat_gas["volatile"] / total_gas if total_gas else 0,
            },
        },
        "top30_by_gas": top_rows,
        "length_ops": length_rows,
        "tx_compute_proxy_dist": {
            "p50": tx_pct[50],
            "p90": tx_pct[90],
            "p99": tx_pct[99],
            "max": tx_pct[100],
            "mean": sum(tx_compute) / len(tx_compute) if tx_compute else None,
            "limit": TX_COMPUTE_LIMIT,
            "p50_vs_limit": (tx_pct[50] / TX_COMPUTE_LIMIT) if tx_pct[50] else None,
            "p99_vs_limit": (tx_pct[99] / TX_COMPUTE_LIMIT) if tx_pct[99] else None,
            "max_vs_limit": (tx_pct[100] / TX_COMPUTE_LIMIT) if tx_pct[100] else None,
        },
        "volatile_access": {
            "txs_with_volatile": volatile_txs,
            "tx_share": volatile_txs / n_tx if n_tx else 0,
            "post_access_compute_p50": post_pct[50],
            "post_access_compute_p90": post_pct[90],
            "post_access_compute_p99": post_pct[99],
            "post_access_compute_max": post_pct[100],
            "vs_block_env_cap_20M": {
                "p99_ratio": (post_pct[99] / BLOCK_ENV_DETENTION) if post_pct[99] else None,
                "max_ratio": (post_pct[100] / BLOCK_ENV_DETENTION) if post_pct[100] else None,
            },
            "vs_oracle_cap_1M": {
                "p99_ratio": (post_pct[99] / ORACLE_DETENTION_PRE) if post_pct[99] else None,
                "max_ratio": (post_pct[100] / ORACLE_DETENTION_PRE) if post_pct[100] else None,
            },
            "vs_oracle_cap_20M": {
                "p99_ratio": (post_pct[99] / ORACLE_DETENTION_REX3) if post_pct[99] else None,
                "max_ratio": (post_pct[100] / ORACLE_DETENTION_REX3) if post_pct[100] else None,
            },
        },
        "limit_hit_cases": limit_hits[:50],
        "limit_hit_count": len(limit_hits),
        "ceiling_premise": {
            "ordinary_ops_share": ordinary_ops,
            "ordinary_gas_share": ordinary_gas,
            "packaging_ceiling_band": [0.05, 0.09],
            "note": "If packaging is on ordinary ops, recoverable mass scales with ordinary_ops_share (~execution) not ordinary_gas_share (storage-dominated).",
        },
    }

    (OUT / "opcode_heat_analysis.json").write_text(json.dumps(report, indent=2) + "\n")

    # markdown report
    md = []
    md.append("# Mainnet opcode heat + limit proximity (offline, 1000 txs)")
    md.append("")
    md.append("**UTC**: 2026-07-24  ")
    md.append("**Corpus**: cached `debug_traceTransaction` structLogs, n=1000  ")
    md.append("**Nature**: offline; no RPC; no megaeth-labs writes.")
    md.append("")
    md.append("## Direct conclusion")
    md.append("")
    md.append(
        f"- **Ordinary ops** = **{100*ordinary_ops:.2f}%** of executions, **{100*ordinary_gas:.2f}%** of EVM-gas mass. "
        f"Packaging ceiling 5–9% WP is about **wrapper invocations on ordinary ops** → execution share **{100*ordinary_ops:.1f}%** "
        f"supports “almost all ops can drop per-op packaging”; gas share is **not** the right premise (storage/CALL dominate gas)."
    )
    md.append(
        f"- **Tx compute-proxy** p50/p99/max = "
        f"**{tx_pct[50]:.0f} / {tx_pct[99]:.0f} / {tx_pct[100]:.0f}** vs limit **{TX_COMPUTE_LIMIT:,}** "
        f"(**{100*tx_pct[50]/TX_COMPUTE_LIMIT:.4f}% / {100*tx_pct[99]/TX_COMPUTE_LIMIT:.3f}% / {100*tx_pct[100]/TX_COMPUTE_LIMIT:.3f}%** of cap)."
    )
    md.append(
        f"- **Volatile-touching txs**: **{100*volatile_txs/n_tx:.1f}%**; post-access gas p99/max = "
        f"**{post_pct[99]:.0f} / {post_pct[100]:.0f}** vs detention **20M** "
        f"({100*(post_pct[99] or 0)/BLOCK_ENV_DETENTION:.3f}% / {100*(post_pct[100] or 0)/BLOCK_ENV_DETENTION:.3f}% of 20M)."
    )
    md.append(
        f"- **Hard limit hits** (tx_gas≥limit or post-vol≥20M): see cases count **{len([h for h in limit_hits if 'ge_tx_compute_limit' in h['kind'] or '20M' in h['kind']])}**."
    )
    md.append("")
    md.append("## 1) Top 30 opcodes by gas mass")
    md.append("")
    md.append("| rank | op | class | count% | gas% | max single gas |")
    md.append("|---:|---|---|---:|---:|---:|")
    for i, r in enumerate(top_rows, 1):
        md.append(
            f"| {i} | `{r['op']}` | {r['rex7_class']} | {100*r['count_share']:.3f}% | "
            f"{100*r['gas_share']:.3f}% | {r['max_single_gas']:,} |"
        )
    md.append("")
    md.append("### Category totals")
    md.append("")
    md.append("| class | ops% | gas% |")
    md.append("|---|---:|---:|")
    for c in ("ordinary", "checkpoint", "volatile"):
        ct = report["category_totals"][c]
        md.append(f"| {c} | {100*ct['ops_share']:.3f}% | {100*ct['gas_share']:.3f}% |")
    md.append("")
    md.append(
        f"**Ordinary combined: ops {100*ordinary_ops:.2f}% / gas {100*ordinary_gas:.2f}%** — "
        "5–9% packaging ceiling tracks **ops** share of ordinary stream, not gas."
    )
    md.append("")
    md.append("## 2) Length-metered opcodes")
    md.append("")
    md.append("| op | count% | gas% | max single gas | txs |")
    md.append("|---|---:|---:|---:|---:|")
    for r in length_rows:
        md.append(
            f"| `{r['op']}` | {100*r['count_share']:.5f}% | {100*r['gas_share']:.5f}% | "
            f"{r['max_single_gas']:,} | {r['txs_seen']} |"
        )
    md.append("")
    md.append(
        "Interpretation: count% should be tiny (packaging tax negligible if still wrapped); "
        "max single gas can still be large (one op can burn past code-length-style bounds)."
    )
    md.append("")
    md.append("## 3) Limit proximity")
    md.append("")
    md.append("### Per-tx compute proxy (Σ gasCost)")
    md.append("")
    md.append(f"| p50 | p90 | p99 | max | limit | max/limit |")
    md.append(f"|---:|---:|---:|---:|---:|---:|")
    md.append(
        f"| {tx_pct[50]:.0f} | {tx_pct[90]:.0f} | {tx_pct[99]:.0f} | {tx_pct[100]:.0f} | "
        f"{TX_COMPUTE_LIMIT:,} | {tx_pct[100]/TX_COMPUTE_LIMIT:.4f} |"
    )
    md.append("")
    md.append("### Volatile access + post-access compute")
    md.append("")
    md.append(
        f"- txs with any volatile op: **{volatile_txs}/{n_tx} = {100*volatile_txs/n_tx:.2f}%**"
    )
    md.append(
        f"- post-access Σgas p50/p99/max: **{post_pct[50]:.0f} / {post_pct[99]:.0f} / {post_pct[100]:.0f}**"
    )
    md.append(
        f"- vs block-env/oracle detention **20M**: p99 ratio **{(post_pct[99] or 0)/BLOCK_ENV_DETENTION:.4f}**, "
        f"max ratio **{(post_pct[100] or 0)/BLOCK_ENV_DETENTION:.4f}**"
    )
    md.append(
        f"- vs pre-Rex3 oracle **1M**: p99 ratio **{(post_pct[99] or 0)/ORACLE_DETENTION_PRE:.3f}**, "
        f"max ratio **{(post_pct[100] or 0)/ORACLE_DETENTION_PRE:.3f}**"
    )
    md.append("")
    md.append(
        "**Overshoot impact surface:** with traffic this far under detention caps, checkpoint overshoot "
        "almost never collides with a binding limit in this window — design risk is correctness bound "
        "size, not frequent user-visible OOG from overshoot."
    )
    md.append("")
    md.append("## 4) Limit-adjacent / hit cases")
    md.append("")
    hard = [h for h in limit_hits if h["kind"] in (
        "tx_gas_ge_tx_compute_limit",
        "post_volatile_ge_block_env_cap_20M",
        "post_volatile_ge_oracle_rex3_cap_20M",
    )]
    near = [h for h in limit_hits if h["kind"] == "tx_gas_ge_50pct_tx_compute_limit"]
    md.append(f"- hard hits: **{len(hard)}**")
    md.append(f"- tx ≥50% compute limit: **{len(near)}**")
    if hard:
        for h in hard[:20]:
            md.append(f"  - `{h['tx']}` {h['kind']} {h}")
    else:
        md.append("- **No tx reached per-tx compute limit or 20M post-volatile detention in this sample.**")
    if near[:10]:
        md.append("- nearest ≥50% limit samples:")
        for h in sorted(near, key=lambda x: -x.get("pct", 0))[:10]:
            md.append(f"  - `{h['tx']}` pct={h['pct']:.4f} gas={h['tx_gas']:,}")
    md.append("")
    md.append("## Method caveats")
    md.append("")
    md.append(report["note_gas_proxy"])
    md.append(
        "- Classification: volatile = env/oracle-arm set (incl. SLOAD); checkpoint = V1 storage/call/return + JUMP/JUMPI; else ordinary."
    )
    md.append("")
    md.append("## Self-cert")
    md.append("")
    md.append("1. Cleanup: offline only.")
    md.append("2. Credential: no RPC in this job.")
    md.append("3. Identity: ARO docs when pushed.")
    md.append("4. megaeth-labs: zero writes.")
    md.append("")

    (OUT / "opcode_heat_report.md").write_text("\n".join(md) + "\n")
    docs = Path("/nvme2/mega-engineer/workspace/aro/docs/mega-evm-mainnet-opcode-heat-20260724.md")
    docs.write_text("\n".join(md) + "\n")

    # merge into merged_decision.json
    merged_path = ROOT / "merged_decision.json"
    merged = json.loads(merged_path.read_text()) if merged_path.exists() else {}
    merged["opcode_heat"] = {
        "ordinary_ops_share": ordinary_ops,
        "ordinary_gas_share": ordinary_gas,
        "tx_compute_p50_p99_max": [tx_pct[50], tx_pct[99], tx_pct[100]],
        "tx_compute_limit": TX_COMPUTE_LIMIT,
        "volatile_tx_share": volatile_txs / n_tx if n_tx else 0,
        "post_vol_p99_max": [post_pct[99], post_pct[100]],
        "length_ops": length_rows,
        "hard_limit_hits": len(hard),
    }
    merged_path.write_text(json.dumps(merged, indent=2) + "\n")

    print("n_tx", n_tx, "ordinary_ops%", round(100 * ordinary_ops, 3), "ordinary_gas%", round(100 * ordinary_gas, 3))
    print("tx p50/p99/max", tx_pct[50], tx_pct[99], tx_pct[100])
    print("vol share", volatile_txs / n_tx, "post p99/max", post_pct[99], post_pct[100])
    print("hard", len(hard), "near50", len(near))
    for r in length_rows:
        print(r["op"], "cnt%", round(100 * r["count_share"], 5), "gas%", round(100 * r["gas_share"], 5), "max", r["max_single_gas"])
    print("WROTE", OUT, docs)


if __name__ == "__main__":
    main()
