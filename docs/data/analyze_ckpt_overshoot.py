#!/usr/bin/env python3
"""Post-process ckpt_overshoot_probe JSONL → V1/V2/V3 decision tables."""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# Opcode bytes
OP = {
    "STOP": 0x00,
    "ADD": 0x01,
    "MUL": 0x02,
    "SUB": 0x03,
    "JUMP": 0x56,
    "JUMPI": 0x57,
    "JUMPDEST": 0x5B,
    "SLOAD": 0x54,
    "SSTORE": 0x55,
    "LOG0": 0xA0,
    "LOG1": 0xA1,
    "LOG2": 0xA2,
    "LOG3": 0xA3,
    "LOG4": 0xA4,
    "CREATE": 0xF0,
    "CALL": 0xF1,
    "CALLCODE": 0xF2,
    "RETURN": 0xF3,
    "DELEGATECALL": 0xF4,
    "CREATE2": 0xF5,
    "STATICCALL": 0xFA,
    "REVERT": 0xFD,
    "SELFDESTRUCT": 0xFF,
    "TIMESTAMP": 0x42,
    "NUMBER": 0x43,
    "COINBASE": 0x41,
    "DIFFICULTY": 0x44,  # PREVRANDAO
    "GASLIMIT": 0x45,
    "CHAINID": 0x46,
    "SELFBALANCE": 0x47,
    "BASEFEE": 0x48,
    "BLOBHASH": 0x49,
    "BLOBBASEFEE": 0x4A,
    "BLOCKHASH": 0x40,
    "BALANCE": 0x31,
    "ORIGIN": 0x32,
    "GASPRICE": 0x3A,
    "EXTCODESIZE": 0x3B,
    "EXTCODECOPY": 0x3C,
    "EXTCODEHASH": 0x3F,
}

CALL_FAMILY = {OP["CALL"], OP["CALLCODE"], OP["DELEGATECALL"], OP["STATICCALL"]}
RETURN_CLASS = {OP["RETURN"], OP["REVERT"], OP["STOP"], OP["SELFDESTRUCT"]}
STORAGE_GAS = {
    OP["SSTORE"],
    OP["LOG0"],
    OP["LOG1"],
    OP["LOG2"],
    OP["LOG3"],
    OP["LOG4"],
    OP["CREATE"],
    OP["CREATE2"],
    OP["SELFDESTRUCT"],
}
# Volatile / detention-relevant env reads (must remain checkpoints).
# SLOAD included: Rex3+ oracle detention path may arm on oracle SLOAD.
VOLATILE = {
    OP["TIMESTAMP"],
    OP["NUMBER"],
    OP["COINBASE"],
    OP["DIFFICULTY"],
    OP["GASLIMIT"],
    OP["CHAINID"],
    OP["SELFBALANCE"],
    OP["BASEFEE"],
    OP["BLOBHASH"],
    OP["BLOBBASEFEE"],
    OP["BLOCKHASH"],
    OP["BALANCE"],
    OP["ORIGIN"],
    OP["GASPRICE"],
    OP["EXTCODESIZE"],
    OP["EXTCODECOPY"],
    OP["EXTCODEHASH"],
    OP["SLOAD"],
}
JUMPS = {OP["JUMP"], OP["JUMPI"]}
JUMPDEST = {OP["JUMPDEST"]}

V1 = CALL_FAMILY | RETURN_CLASS | STORAGE_GAS | VOLATILE
V2 = V1 | JUMPS
V3 = V2 | JUMPDEST

# T2a packaging ceiling band (whole-program cycles if packaging→0)
CEIL_LO, CEIL_HI = 0.05, 0.09


def weighted_percentile(values_weights, ps):
    """values_weights: list of (value, weight). weight>0."""
    items = [(float(v), float(w)) for v, w in values_weights if w > 0]
    if not items:
        return {p: None for p in ps}
    items.sort(key=lambda x: x[0])
    total = sum(w for _, w in items)
    out = {}
    for p in ps:
        if total <= 0:
            out[p] = None
            continue
        target = (p / 100.0) * total
        acc = 0.0
        val = items[-1][0]
        for v, w in items:
            acc += w
            if acc >= target:
                val = v
                break
        out[p] = val
    return out


def op_name(op: int) -> str:
    for k, v in OP.items():
        if v == op:
            return k
    return f"OP_{op:02x}"


def segments_for(steps, ckpt_set):
    """Return list of dicts: gas, n_ops, ops(list), starts after checkpoint inclusive-end exclusive.

    Segment gas = sum of non-checkpoint opcode gas between checkpoints.
    Checkpoint opcodes themselves are boundaries (not inside segment body).
    Leading ops before first ckpt form a segment; trailing after last ckpt too.
    """
    segs = []
    cur_gas = 0
    cur_ops = []
    for op, gas, depth in steps:
        if op in ckpt_set:
            if cur_ops or cur_gas:
                segs.append({"gas": cur_gas, "n_ops": len(cur_ops), "ops": cur_ops[:]})
            cur_gas = 0
            cur_ops = []
            # checkpoint itself is not a segment body
        else:
            cur_gas += gas
            cur_ops.append(op)
    if cur_ops or cur_gas:
        segs.append({"gas": cur_gas, "n_ops": len(cur_ops), "ops": cur_ops[:]})
    return segs


def analyze(path: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    workloads = {}  # wl -> list of step lists (per tx)
    metas = []
    cur_wl = None
    cur_tx = None
    cur_steps = []

    def flush():
        nonlocal cur_steps, cur_wl, cur_tx
        if cur_wl is not None and cur_steps is not None:
            workloads.setdefault(cur_wl, []).append(cur_steps)
        cur_steps = []

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        t = o.get("type")
        if t == "run_meta":
            metas.append(o)
        elif t == "meta":
            metas.append(o)
        elif t == "step":
            wl, tx = o["wl"], o["tx"]
            if cur_wl != wl or cur_tx != tx:
                flush()
                cur_wl, cur_tx = wl, tx
                cur_steps = []
            cur_steps.append((int(o["op"]), int(o["gas"]), int(o.get("depth", 0))))
        elif t == "tx_end":
            flush()
            cur_wl = cur_tx = None
            # store ok stats on last
            workloads.setdefault("_tx_end", []).append(o)
    flush()

    variants = {"V1": V1, "V2": V2, "V3": V3}
    decision_rows = []
    longest_cases = []
    dist_rows = []

    # global pooled per variant
    for vname, ckpt in variants.items():
        all_segs = []  # (gas, weight=gas, wl)
        total_ops = 0
        ckpt_ops = 0
        total_gas = 0
        ckpt_gas = 0
        per_wl = {}

        for wl, txs in workloads.items():
            if wl.startswith("_"):
                continue
            wl_segs = []
            w_ops = w_ckpt_ops = w_gas = w_ckpt_gas = 0
            for steps in txs:
                for op, gas, _ in steps:
                    w_ops += 1
                    w_gas += gas
                    if op in ckpt:
                        w_ckpt_ops += 1
                        w_ckpt_gas += gas
                segs = segments_for(steps, ckpt)
                for s in segs:
                    wl_segs.append(s)
                    all_segs.append((s["gas"], s["gas"], wl, s))
            per_wl[wl] = {
                "segs": wl_segs,
                "ops": w_ops,
                "ckpt_ops": w_ckpt_ops,
                "gas": w_gas,
                "ckpt_gas": w_ckpt_gas,
            }
            total_ops += w_ops
            ckpt_ops += w_ckpt_ops
            total_gas += w_gas
            ckpt_gas += w_ckpt_gas

        # gas-weighted percentiles of segment gas (= overshoot envelope)
        ww = [(g, w) for g, w, _, _ in all_segs]
        pct = weighted_percentile(ww, [50, 90, 99, 100])
        # also unweighted
        bare = [g for g, _, _, _ in all_segs]
        bare_sorted = sorted(bare) if bare else []

        def bare_p(p):
            if not bare_sorted:
                return None
            if p >= 100:
                return bare_sorted[-1]
            idx = min(len(bare_sorted) - 1, max(0, int(math.ceil(p / 100.0 * len(bare_sorted)) - 1)))
            return bare_sorted[idx]

        tax_ops = (ckpt_ops / total_ops) if total_ops else None
        tax_gas = (ckpt_gas / total_gas) if total_gas else None
        # Packaging residual ≈ per-opcode wrapper invocations (op-count).
        # Gas mass at checkpoints is storage-dominated, NOT packaging residual.
        # Recoverable packaging ceiling ≈ (1 - residual_tax_ops) * T2a 5–9% WP band.
        rec_lo = None if tax_ops is None else CEIL_LO * (1.0 - tax_ops)
        rec_hi = None if tax_ops is None else CEIL_HI * (1.0 - tax_ops)

        # longest segment
        longest = max(all_segs, key=lambda x: x[0], default=None)
        if longest:
            g, _, wl, s = longest
            # pattern: top opcodes in segment
            c = Counter(s["ops"])
            top = [(op_name(op), n) for op, n in c.most_common(8)]
            longest_cases.append(
                {
                    "variant": vname,
                    "wl": wl,
                    "seg_gas": g,
                    "seg_n_ops": s["n_ops"],
                    "top_ops": top,
                }
            )

        row = {
            "variant": vname,
            "n_segments": len(all_segs),
            "overshoot_p50_gas_wt": pct[50],
            "overshoot_p90_gas_wt": pct[90],
            "overshoot_p99_gas_wt": pct[99],
            "overshoot_max_gas_wt": pct[100],
            "overshoot_p50_unweighted": bare_p(50),
            "overshoot_p99_unweighted": bare_p(99),
            "overshoot_max_unweighted": bare_p(100),
            "residual_tax_ops": tax_ops,
            "ckpt_gas_mass_frac": tax_gas,
            "recoverable_ceil_lo": rec_lo,
            "recoverable_ceil_hi": rec_hi,
            "total_ops": total_ops,
            "ckpt_ops": ckpt_ops,
            "total_gas": total_gas,
            "ckpt_gas": ckpt_gas,
        }
        decision_rows.append(row)

        # distribution histogram (log buckets)
        buckets = Counter()
        for g, w, _, _ in all_segs:
            if g <= 0:
                b = "0"
            else:
                b = f"2^{int(math.floor(math.log2(g)))}"
            buckets[b] += w
        dist_rows.append({"variant": vname, "gas_weighted_hist": dict(sorted(buckets.items(), key=lambda x: x[0]))})

        # per-wl breakdown
        wl_break = []
        for wl, st in sorted(per_wl.items()):
            segs = st["segs"]
            ww2 = [(s["gas"], s["gas"]) for s in segs]
            p2 = weighted_percentile(ww2, [50, 99, 100])
            tax = (st["ckpt_gas"] / st["gas"]) if st["gas"] else None
            wl_break.append(
                {
                    "wl": wl,
                    "n_seg": len(segs),
                    "p50": p2[50],
                    "p99": p2[99],
                    "max": p2[100],
                    "tax_gas": tax,
                    "ops": st["ops"],
                    "gas": st["gas"],
                }
            )
        (out_dir / f"per_wl_{vname}.json").write_text(json.dumps(wl_break, indent=2) + "\n")

    report = {
        "method": {
            "trace": "revm Inspector step/step_end gas remaining delta; CALL/CREATE child gas_limit stripped",
            "overshoot_def": "gas-weighted distribution of inter-checkpoint straight-line segment gas; max overshoot envelope = segment gas",
            "residual_tax_ops": "fraction of executed opcodes that remain checkpoints (still need packaging wrappers)",
            "ckpt_gas_mass_frac": "fraction of EVM gas spent ON checkpoint opcodes (storage-dominated; not packaging residual)",
            "recoverable_ceil": "(1 - residual_tax_ops) * T2a packaging ceiling band 5-9% WP cycles",
            "variants": {
                "V1": "CALL-family + RETURN-class + storage-gas + volatile/detention",
                "V2": "V1 + JUMP/JUMPI",
                "V3": "V2 + JUMPDEST",
            },
            "non_negotiable": "volatile/detention + storage-gas in all variants",
        },
        "decision_table": decision_rows,
        "longest_segments": longest_cases,
        "distributions": dist_rows,
        "workloads": [m for m in metas if m.get("type") == "meta"],
        "run_meta": [m for m in metas if m.get("type") == "run_meta"],
        "representativeness": {
            "included": sorted([k for k in workloads if not k.startswith("_")]),
            "rpc_live_tx": False,
            "eest_fixtures": False,
            "boundary": "Synthetic + ARO probe-shaped bytecode only; no mainnet replay (RPC rate-limited). REX6 7702/system paths approximated. Numbers are design-grade for relative V1/V2/V3 ranking, not absolute mainnet quantiles.",
        },
    }
    (out_dir / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")

    # CSV decision table
    cols = [
        "variant",
        "overshoot_p50_gas_wt",
        "overshoot_p99_gas_wt",
        "overshoot_max_gas_wt",
        "residual_tax_ops",
        "ckpt_gas_mass_frac",
        "recoverable_ceil_lo",
        "recoverable_ceil_hi",
        "n_segments",
    ]
    lines = [",".join(cols)]
    for r in decision_rows:
        lines.append(",".join(str(r.get(c, "")) for c in cols))
    (out_dir / "decision_table.csv").write_text("\n".join(lines) + "\n")

    # human markdown table fragment
    md = [
        "| variant | overshoot p50/p99/max (gas, wt) | residual tax (ops) | ckpt gas-mass | recoverable ceil band |",
        "|---|---|---|---|---|",
    ]
    for r in decision_rows:
        md.append(
            f"| {r['variant']} | "
            f"{r['overshoot_p50_gas_wt']:.0f} / {r['overshoot_p99_gas_wt']:.0f} / {r['overshoot_max_gas_wt']:.0f} | "
            f"{100*r['residual_tax_ops']:.2f}% | "
            f"{100*r['ckpt_gas_mass_frac']:.2f}% | "
            f"{100*r['recoverable_ceil_lo']:.2f}–{100*r['recoverable_ceil_hi']:.2f}% WP |"
        )
    (out_dir / "decision_table.md").write_text("\n".join(md) + "\n")
    print("WROTE", out_dir)
    print("\n".join(md))
    return report


if __name__ == "__main__":
    import sys

    inp = Path(sys.argv[1] if len(sys.argv) > 1 else "stream.jsonl")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "analysis")
    analyze(inp, out)
