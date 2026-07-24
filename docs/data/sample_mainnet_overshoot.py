#!/usr/bin/env python3
"""Sample mainnet txs via eth_/debug_ read RPCs and compute V1/V2/V3 overshoot.

Credential: MEGA_RPC_URL env only — never argv, never disk logs.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

# --- opcode name -> byte (subset + full common set) ---
OP_BY_NAME = {
    "STOP": 0x00,
    "ADD": 0x01,
    "MUL": 0x02,
    "SUB": 0x03,
    "DIV": 0x04,
    "SDIV": 0x05,
    "MOD": 0x06,
    "SMOD": 0x07,
    "ADDMOD": 0x08,
    "MULMOD": 0x09,
    "EXP": 0x0A,
    "SIGNEXTEND": 0x0B,
    "LT": 0x10,
    "GT": 0x11,
    "SLT": 0x12,
    "SGT": 0x13,
    "EQ": 0x14,
    "ISZERO": 0x15,
    "AND": 0x16,
    "OR": 0x17,
    "XOR": 0x18,
    "NOT": 0x19,
    "BYTE": 0x1A,
    "SHL": 0x1B,
    "SHR": 0x1C,
    "SAR": 0x1D,
    "SHA3": 0x20,
    "KECCAK256": 0x20,
    "ADDRESS": 0x30,
    "BALANCE": 0x31,
    "ORIGIN": 0x32,
    "CALLER": 0x33,
    "CALLVALUE": 0x34,
    "CALLDATALOAD": 0x35,
    "CALLDATASIZE": 0x36,
    "CALLDATACOPY": 0x37,
    "CODESIZE": 0x38,
    "CODECOPY": 0x39,
    "GASPRICE": 0x3A,
    "EXTCODESIZE": 0x3B,
    "EXTCODECOPY": 0x3C,
    "RETURNDATASIZE": 0x3D,
    "RETURNDATACOPY": 0x3E,
    "EXTCODEHASH": 0x3F,
    "BLOCKHASH": 0x40,
    "COINBASE": 0x41,
    "TIMESTAMP": 0x42,
    "NUMBER": 0x43,
    "DIFFICULTY": 0x44,
    "PREVRANDAO": 0x44,
    "GASLIMIT": 0x45,
    "CHAINID": 0x46,
    "SELFBALANCE": 0x47,
    "BASEFEE": 0x48,
    "BLOBHASH": 0x49,
    "BLOBBASEFEE": 0x4A,
    "POP": 0x50,
    "MLOAD": 0x51,
    "MSTORE": 0x52,
    "MSTORE8": 0x53,
    "SLOAD": 0x54,
    "SSTORE": 0x55,
    "JUMP": 0x56,
    "JUMPI": 0x57,
    "PC": 0x58,
    "MSIZE": 0x59,
    "GAS": 0x5A,
    "JUMPDEST": 0x5B,
    "TLOAD": 0x5C,
    "TSTORE": 0x5D,
    "MCOPY": 0x5E,
    "PUSH0": 0x5F,
    "DUP1": 0x80,
    "SWAP1": 0x90,
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
    "INVALID": 0xFE,
    "SELFDESTRUCT": 0xFF,
}
for i in range(1, 33):
    OP_BY_NAME[f"PUSH{i}"] = 0x5F + i
for i in range(1, 17):
    OP_BY_NAME[f"DUP{i}"] = 0x7F + i
    OP_BY_NAME[f"SWAP{i}"] = 0x8F + i

CALL_FAMILY = {0xF1, 0xF2, 0xF4, 0xFA}
RETURN_CLASS = {0xF3, 0xFD, 0x00, 0xFF}
STORAGE_GAS = {0x55, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xF0, 0xF5, 0xFF}
VOLATILE = {
    0x42, 0x43, 0x41, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x40, 0x31, 0x32, 0x3A, 0x3B, 0x3C, 0x3F, 0x54,
}
JUMPS = {0x56, 0x57}
JUMPDEST = {0x5B}
V1 = CALL_FAMILY | RETURN_CLASS | STORAGE_GAS | VOLATILE
V2 = V1 | JUMPS
V3 = V2 | JUMPDEST
CEIL_LO, CEIL_HI = 0.05, 0.09

TARGET_TXS = int(os.environ.get("TARGET_TXS", "1000"))
MAX_BLOCKS = int(os.environ.get("MAX_BLOCKS", "800"))
SLEEP_S = float(os.environ.get("RPC_SLEEP", "0.08"))
BATCH = int(os.environ.get("RPC_BATCH", "20"))


def rpc_url() -> str:
    u = os.environ.get("MEGA_RPC_URL") or os.environ.get("RPC_URL")
    if not u:
        raise SystemExit("MEGA_RPC_URL not set")
    return u


def rpc_call(method, params, timeout=120, retries=5):
    url = rpc_url()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            if "error" in data:
                err = data["error"]
                msg = str(err)
                # rate limit backoff
                if "rate" in msg.lower() or "limit" in msg.lower() or err.get("code") == -32005:
                    time.sleep(1.5 * (attempt + 1))
                    last = err
                    continue
                return None, err
            return data.get("result"), None
        except Exception as e:
            last = str(type(e).__name__)
            time.sleep(0.5 * (attempt + 1))
    return None, last


def rpc_batch(calls, timeout=180, retries=5):
    """calls: list of (method, params) -> list of results/errors aligned."""
    url = rpc_url()
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)
    ]
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            if not isinstance(data, list):
                if isinstance(data, dict) and "error" in data:
                    err = data["error"]
                    if "rate" in str(err).lower() or err.get("code") == -32005:
                        time.sleep(1.5 * (attempt + 1))
                        last = err
                        continue
                return [None] * len(calls), last or "bad_batch"
            by_id = {item.get("id"): item for item in data if isinstance(item, dict)}
            out = []
            for i in range(len(calls)):
                item = by_id.get(i, {})
                if "error" in item:
                    out.append(("err", item["error"]))
                else:
                    out.append(("ok", item.get("result")))
            return out, None
        except Exception as e:
            last = type(e).__name__
            time.sleep(0.8 * (attempt + 1))
    return [None] * len(calls), last


def op_byte(name: str) -> int | None:
    if not name:
        return None
    return OP_BY_NAME.get(name.upper())


def weighted_percentile(values_weights, ps):
    items = [(float(v), float(w)) for v, w in values_weights if w > 0]
    if not items:
        return {p: None for p in ps}
    items.sort(key=lambda x: x[0])
    total = sum(w for _, w in items)
    out = {}
    for p in ps:
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


def segments_for(steps, ckpt_set):
    segs = []
    cur_gas = 0
    cur_ops = []
    for op, gas in steps:
        if op in ckpt_set:
            if cur_ops or cur_gas:
                segs.append({"gas": cur_gas, "n_ops": len(cur_ops), "ops": cur_ops[:]})
            cur_gas = 0
            cur_ops = []
        else:
            cur_gas += gas
            cur_ops.append(op)
    if cur_ops or cur_gas:
        segs.append({"gas": cur_gas, "n_ops": len(cur_ops), "ops": cur_ops[:]})
    return segs


def steps_from_structlogs(struct_logs):
    steps = []
    unknown = Counter()
    for lg in struct_logs:
        name = lg.get("op") or ""
        b = op_byte(name)
        if b is None:
            unknown[name] += 1
            continue
        cost = int(lg.get("gasCost") or 0)
        steps.append((b, cost))
    return steps, unknown


def analyze_steps_list(all_tx_steps, label):
    variants = {"V1": V1, "V2": V2, "V3": V3}
    rows = []
    longest = []
    for vname, ckpt in variants.items():
        all_segs = []
        total_ops = ckpt_ops = total_gas = ckpt_gas = 0
        for steps in all_tx_steps:
            for op, gas in steps:
                total_ops += 1
                total_gas += gas
                if op in ckpt:
                    ckpt_ops += 1
                    ckpt_gas += gas
            for s in segments_for(steps, ckpt):
                all_segs.append((s["gas"], s["gas"], s))
        pct = weighted_percentile([(g, w) for g, w, _ in all_segs], [50, 90, 99, 100])
        tax_ops = (ckpt_ops / total_ops) if total_ops else None
        tax_gas = (ckpt_gas / total_gas) if total_gas else None
        rec_lo = None if tax_ops is None else CEIL_LO * (1.0 - tax_ops)
        rec_hi = None if tax_ops is None else CEIL_HI * (1.0 - tax_ops)
        longest_s = max(all_segs, key=lambda x: x[0], default=None)
        if longest_s:
            g, _, s = longest_s
            c = Counter(s["ops"])
            top = [(f"0x{op:02x}", n) for op, n in c.most_common(8)]
            longest.append(
                {"variant": vname, "label": label, "seg_gas": g, "seg_n_ops": s["n_ops"], "top_ops": top}
            )
        rows.append(
            {
                "source": label,
                "variant": vname,
                "n_segments": len(all_segs),
                "n_txs": len(all_tx_steps),
                "overshoot_p50_gas_wt": pct[50],
                "overshoot_p90_gas_wt": pct[90],
                "overshoot_p99_gas_wt": pct[99],
                "overshoot_max_gas_wt": pct[100],
                "residual_tax_ops": tax_ops,
                "ckpt_gas_mass_frac": tax_gas,
                "recoverable_ceil_lo": rec_lo,
                "recoverable_ceil_hi": rec_hi,
                "total_ops": total_ops,
                "ckpt_ops": ckpt_ops,
                "total_gas": total_gas,
            }
        )
    return rows, longest


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "mainnet_overshoot")
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "trace_cache"
    cache_dir.mkdir(exist_ok=True)

    # meta without secrets
    head, err = rpc_call("eth_blockNumber", [])
    if err or not head:
        print("FATAL blockNumber", err, file=sys.stderr)
        return 2
    latest = int(head, 16)
    chain, _ = rpc_call("eth_chainId", [])
    print(f"latest_block={latest} chainId={chain} target_txs={TARGET_TXS}", flush=True)

    # collect tx hashes walking backward
    hashes = []
    block_first = latest
    block_last = latest
    b = latest
    blocks_seen = 0
    while len(hashes) < TARGET_TXS * 2 and blocks_seen < MAX_BLOCKS and b > 0:
        # batch get blocks
        batch_nums = []
        for _ in range(min(BATCH, MAX_BLOCKS - blocks_seen)):
            if b <= 0:
                break
            batch_nums.append(b)
            b -= 1
        calls = [("eth_getBlockByNumber", [hex(n), False]) for n in batch_nums]
        results, berr = rpc_batch(calls)
        if berr:
            print(f"block_batch_err={berr}", flush=True)
            time.sleep(2)
            continue
        for n, item in zip(batch_nums, results):
            blocks_seen += 1
            if not item or item[0] != "ok" or not item[1]:
                continue
            blk = item[1]
            txs = blk.get("transactions") or []
            if not txs:
                continue
            block_last = min(block_last, n)
            block_first = max(block_first, n)
            for th in txs:
                hashes.append((th, n))
        time.sleep(SLEEP_S)
        if blocks_seen % 50 == 0:
            print(f"progress_blocks={blocks_seen} hashes={len(hashes)}", flush=True)

    # shuffle then take stable sample; prefer diversity by block
    random.seed(42)
    random.shuffle(hashes)
    # de-dup preserve order
    seen = set()
    ordered = []
    for th, bn in hashes:
        if th in seen:
            continue
        seen.add(th)
        ordered.append((th, bn))

    print(f"hash_pool={len(ordered)} blocks_scanned={blocks_seen}", flush=True)

    all_steps = []
    tx_meta = []
    unknown_ops = Counter()
    fail = Counter()
    checkpoint_stats = []  # for stability

    for i, (th, bn) in enumerate(ordered):
        if len(all_steps) >= TARGET_TXS:
            break
        cache_path = cache_dir / f"{th}.json"
        struct_logs = None
        if cache_path.exists():
            try:
                struct_logs = json.loads(cache_path.read_text()).get("structLogs")
            except Exception:
                struct_logs = None
        if struct_logs is None:
            tr, err = rpc_call(
                "debug_traceTransaction",
                [
                    th,
                    {
                        "disableMemory": True,
                        "disableStack": True,
                        "disableStorage": True,
                        "enableReturnData": False,
                    },
                ],
                timeout=180,
            )
            time.sleep(SLEEP_S)
            if err or not isinstance(tr, dict):
                fail[str(err)[:80] if err else "null"] += 1
                continue
            struct_logs = tr.get("structLogs") or []
            # cache only opcode stream stripped (no URL)
            slim = {
                "tx": th,
                "block": bn,
                "structLogs": [
                    {"op": lg.get("op"), "gasCost": lg.get("gasCost"), "depth": lg.get("depth"), "pc": lg.get("pc")}
                    for lg in struct_logs
                ],
            }
            cache_path.write_text(json.dumps(slim, separators=(",", ":")))
        steps, unk = steps_from_structlogs(struct_logs)
        unknown_ops.update(unk)
        if not steps:
            fail["empty_steps"] += 1
            continue
        all_steps.append(steps)
        tx_meta.append({"tx": th, "block": bn, "n_ops": len(steps), "gas_sum": sum(g for _, g in steps)})
        if len(all_steps) in (100, 200, 500, 1000) or len(all_steps) == TARGET_TXS:
            rows, _ = analyze_steps_list(all_steps, f"mainnet_n={len(all_steps)}")
            v1 = next(r for r in rows if r["variant"] == "V1")
            checkpoint_stats.append(
                {
                    "n": len(all_steps),
                    "v1_p50": v1["overshoot_p50_gas_wt"],
                    "v1_p99": v1["overshoot_p99_gas_wt"],
                    "v1_max": v1["overshoot_max_gas_wt"],
                    "v1_tax_ops": v1["residual_tax_ops"],
                }
            )
            print(
                f"n={len(all_steps)} V1_p50={v1['overshoot_p50_gas_wt']} V1_p99={v1['overshoot_p99_gas_wt']} V1_max={v1['overshoot_max_gas_wt']} tax_ops={v1['residual_tax_ops']}",
                flush=True,
            )

    rows, longest = analyze_steps_list(all_steps, "mainnet")
    # also intermediate stability rows
    report = {
        "window": {
            "latest_at_start": latest,
            "block_high": block_first,
            "block_low": block_last,
            "blocks_scanned": blocks_seen,
            "chain_id": chain,
            "target_txs": TARGET_TXS,
            "sampled_txs": len(all_steps),
            "hash_pool": len(ordered),
            "fail": dict(fail),
            "unknown_ops": dict(unknown_ops),
        },
        "stability": checkpoint_stats,
        "decision_rows_mainnet": rows,
        "longest_segments": longest,
        "compare_probe_v1_p50_ref": 184,
        "answers": {},
    }
    # answers
    v1 = next(r for r in rows if r["variant"] == "V1")
    v2 = next(r for r in rows if r["variant"] == "V2")
    v3 = next(r for r in rows if r["variant"] == "V3")
    report["answers"] = {
        "long_straight_on_mainnet": {
            "v1_p99": v1["overshoot_p99_gas_wt"],
            "v1_max": v1["overshoot_max_gas_wt"],
            "distance_to_50k_bound": None
            if v1["overshoot_max_gas_wt"] is None
            else 50002 - v1["overshoot_max_gas_wt"],
            "note": "max segment gas vs synth 50k adversarial bound",
        },
        "supports_v1_default": {
            "mainnet_v1_p50": v1["overshoot_p50_gas_wt"],
            "probe_shaped_v1_p50_ref": 184,
            "mainnet_v1_p50_vs_probe": None
            if v1["overshoot_p50_gas_wt"] is None
            else v1["overshoot_p50_gas_wt"] / 184.0,
            "v2_p50": v2["overshoot_p50_gas_wt"],
            "v3_p50": v3["overshoot_p50_gas_wt"],
            "residual_tax_ops_v1": v1["residual_tax_ops"],
        },
    }

    (out_dir / "mainnet_analysis.json").write_text(json.dumps(report, indent=2) + "\n")
    # compact tx index without embedding secrets
    (out_dir / "tx_index.json").write_text(
        json.dumps(
            {
                "n": len(tx_meta),
                "block_high": block_first,
                "block_low": block_last,
                "txs": tx_meta,
            },
            indent=2,
        )
        + "\n"
    )
    # stream-like summary jsonl for merging (op steps only, no URL)
    with (out_dir / "mainnet_steps_meta.jsonl").open("w") as f:
        for i, (meta, steps) in enumerate(zip(tx_meta, all_steps)):
            f.write(
                json.dumps(
                    {
                        "type": "tx_end",
                        "wl": "mainnet",
                        "tx_i": i,
                        "tx": meta["tx"],
                        "block": meta["block"],
                        "steps": len(steps),
                        "gas_sum": meta["gas_sum"],
                    }
                )
                + "\n"
            )

    print("WROTE", out_dir, "n_txs", len(all_steps), flush=True)
    for r in rows:
        print(
            r["variant"],
            "p50",
            r["overshoot_p50_gas_wt"],
            "p99",
            r["overshoot_p99_gas_wt"],
            "max",
            r["overshoot_max_gas_wt"],
            "tax_ops",
            r["residual_tax_ops"],
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
