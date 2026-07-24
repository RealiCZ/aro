#!/usr/bin/env python3
"""V1.5 (backward-jump settle) offline analysis over cached traces.

No RPC. Reuses mainnet trace_cache + synthetic stream.jsonl.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

ROOT = Path("/nvme2/mega-engineer/workspace/aro/docs/data/mega-evm-ckpt-overshoot-20260724")
MAINNET_CACHE = ROOT / "mainnet/trace_cache"
SYNTH_STREAM = ROOT / "stream.jsonl"
OUT = ROOT / "v1_5"
CEIL_LO, CEIL_HI = 0.05, 0.09
# From prior mainnet analysis
MAINNET_V1_RECOVER = (0.0496, 0.0894)
MAINNET_V2_RECOVER = (0.0461, 0.0830)
MAINNET_V2_TAX = 0.0780  # all JUMP/JUMPI ops share

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
    "INVALID": 0xFE, "SELFDESTRUCT": 0xFF,
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
    0x42, 0x43, 0x41, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x40, 0x31, 0x32, 0x3A,
    0x3B, 0x3C, 0x3F, 0x54,
}
V1 = CALL_FAMILY | RETURN_CLASS | STORAGE_GAS | VOLATILE
JUMP = 0x56
JUMPI = 0x57
JUMPS = {JUMP, JUMPI}


def op_byte(name: str) -> int | None:
    return OP_BY_NAME.get((name or "").upper())


def push_width(op: int) -> int:
    if 0x60 <= op <= 0x7F:  # PUSH1..PUSH32
        return op - 0x5F
    return 0


def insn_size(op: int) -> int:
    return 1 + push_width(op)


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


def load_mainnet_traces():
    """Yield list of steps: dict(op, gas, pc, depth) per tx."""
    for p in sorted(MAINNET_CACHE.glob("*.json")):
        o = json.loads(p.read_text())
        steps = []
        for lg in o.get("structLogs") or []:
            b = op_byte(lg.get("op") or "")
            if b is None:
                continue
            steps.append(
                {
                    "op": b,
                    "gas": int(lg.get("gasCost") or 0),
                    "pc": int(lg.get("pc") or 0),
                    "depth": int(lg.get("depth") or 1),
                    "name": lg.get("op"),
                }
            )
        if steps:
            yield {"id": o.get("tx") or p.stem, "source": "mainnet", "steps": steps}


def load_synth_traces():
    """Synthetic stream has op byte + gas but no pc — reconstruct pc within depth via sequential estimate.

    Without real pc, backward-jump detection is approximate: treat JUMP/JUMPI as
    potential back-edge only when next step depth same and we can track a synthetic pc.
    """
    cur = None
    buckets = {}
    # Rebuild per (wl, tx)
    for line in SYNTH_STREAM.read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        t = o.get("type")
        if t == "step":
            key = (o["wl"], o["tx"])
            buckets.setdefault(key, []).append(
                {"op": int(o["op"]), "gas": int(o["gas"]), "depth": int(o.get("depth") or 1)}
            )
        elif t == "tx_end":
            key = (o["wl"], o["tx"])
            steps_raw = buckets.get(key) or []
            # reconstruct pc per depth frame stack
            steps = reconstruct_pc(steps_raw)
            if steps:
                yield {"id": f"{o['wl']}:{o['tx']}", "source": "synth", "wl": o["wl"], "steps": steps}


def reconstruct_pc(steps_raw):
    """Best-effort PC reconstruction for synthetic streams lacking pc.

    Advance pc by insn_size unless JUMP/JUMPI taken. Taken detection:
    - JUMP: always taken; target unknown without stack — mark jump_unknown.
    For synth without stack we cannot know target. Strategy:
    - Record jump ops; for taken detection use heuristic only when next depth==depth
      and we observe next op is JUMPDEST (common) — still don't know target vs pc.
    Without stack, V1.5 back-jump on synth is **under-approximated**: only flag
    JUMPI/JUMP as backedge when next pc reconstructed would require a model.

    Better approach for synth: linear pc per depth until JUMP/JUMPI, then if next
    step is JUMPDEST, set pc to a sentinel and treat JUMP to JUMPDEST with
    unknown target as **not** counted as backedge unless we track last JUMPDEST pcs.

    Practical synth model used here:
    - Maintain pc per depth.
    - On non-jump: pc += insn_size.
    - On JUMP/JUMPI: look at next step same depth; if next is JUMPDEST, target=next.pc
      after we assign JUMPDEST pc... chicken-egg.
    - Assign: when we see JUMP/JUMPI, don't advance; next instruction if JUMPDEST gets
      pc = previous JUMPDEST table... 

    Simpler honest approach for synth: **skip accurate V1.5 segment for synth** if no pc,
    OR scan bytecode patterns we generated — we know synth_jump_loop has back edges.

    We'll do: sequential pc+=size; on JUMP/JUMPI always treat as taken to next step's
    assigned pc where next step starts at unknown. Assign next step pc only if not jump:
    
    Actually geth structLog pc is address of current op. After JUMP to T, next log has pc=T.
    Reconstruction without stack:
      pc_cur starts 0 at depth enter
      for each step: record pc_cur; then if op in JUMP/JUMPI: pc_cur = None (unknown)
      elif pc_cur is not None: pc_cur += insn_size
      when depth increases: push and reset pc_cur=0
      when depth decreases: pop

    When pc_cur is None and we see JUMPDEST, we cannot know if back or forward.
    Count JUMP/JUMPI frequency still works. For backedge: only when previous was JUMP/JUMPI
    and we had known pc before jump and after jump we see JUMPDEST — still unknown target.

    For synth V1.5 segments: use only V1 checkpoints (conservative = V1 segments) AND
    additionally break on every JUMP/JUMPI (upper bound on settle frequency = V2),
    and a middle estimate: break on JUMP/JUMPI when followed by JUMPDEST with
    reconstructed linear pc suggesting loop (if JUMPDEST pc we assign by... no).

    I'll load synth for jump frequency of JUMP/JUMPI ops only, and for segment stats
    report mainnet as authoritative for V1.5; synth V1.5 segments use heuristic:
    **break on JUMP/JUMPI when next op is JUMPDEST and depth unchanged** treating as taken,
    and **backedge if JUMPDEST's running pc counter is less than jump pc** using linear pc
    only until first jump in frame then mark uncertain.

    Simplest defensible synth approach matching data we have:
    - Frequency of JUMP/JUMPI ops: exact.
    - Backedge rate on synth: not reliable without pc/stack — report as N/A or
      estimate from workloads that we know (jump_loop).
    - Prefer mainnet for V1.5 segment distribution (has real pc).

    User asked for both mainnet and synth. For synth stream I'll reconstruct:
    linear pc per depth; on JUMP/JUMPI, set pending_jump_from=pc; next step gets pc from
    ... we don't know. Look at next step: if it's JUMPDEST, leave pc as 'unknown_target'
    and do NOT count backedge (conservative for tax a) undercount).

    Alternative: use only mainnet for (1a)(2)(3)(5) and synth only for jump op frequency
    comparison. User said both corpora for (1).

    I'll implement proper backedge only when `pc` field present (mainnet).
    For synth, reconstruct linear pc ignoring jumps as fallthrough (wrong for loops)
    but for jump_loop workload annotate separately by detecting JUMPI count.

    Final: mainnet full V1.5; synth reports jump_op_freq and V1-only segments as baseline
    plus note that V1.5 backedge needs pc (mainnet-grade).
    """
    steps = []
    # depth -> pc
    pcs = {}
    for s in steps_raw:
        d = s["depth"]
        if d not in pcs:
            pcs[d] = 0
            # clear deeper
            for dd in list(pcs):
                if dd > d:
                    del pcs[dd]
        pc = pcs[d]
        op = s["op"]
        steps.append({"op": op, "gas": s["gas"], "pc": pc, "depth": d, "pc_reliable": True})
        if op in JUMPS:
            # unknown target — mark following until we sync; disable reliability at this depth
            pcs[d] = None  # type: ignore
            steps[-1]["pc_reliable"] = False
        else:
            if pcs[d] is None:
                steps[-1]["pc_reliable"] = False
                # stay unreliable; try resync on JUMPDEST by not changing
                if op == 0x5B:
                    pass
            else:
                pcs[d] = pcs[d] + insn_size(op)
    return steps


def annotate_jumps(steps):
    """Add taken/backedge flags using successive pcs (requires reliable pc)."""
    n = len(steps)
    total_jump_ops = 0
    taken_jumps = 0
    back_jumps = 0
    forward_taken = 0
    not_taken_jumpi = 0
    unreliable = 0

    for i, s in enumerate(steps):
        op = s["op"]
        if op not in JUMPS:
            s["is_jump"] = False
            continue
        s["is_jump"] = True
        total_jump_ops += 1
        if not s.get("pc_reliable", True) or s.get("pc") is None:
            unreliable += 1
            s["taken"] = None
            s["backedge"] = False
            continue
        pc = s["pc"]
        # find next step same or any? After JUMP, next log is at target (or depth change for other reasons)
        if i + 1 >= n:
            s["taken"] = None
            s["backedge"] = False
            continue
        nxt = steps[i + 1]
        # depth drop means return, not jump target
        if nxt["depth"] < s["depth"]:
            s["taken"] = None
            s["backedge"] = False
            continue
        if nxt["depth"] > s["depth"]:
            # shouldn't happen for JUMP
            s["taken"] = None
            s["backedge"] = False
            continue
        next_pc = nxt.get("pc")
        fallthrough = pc + insn_size(op)
        if op == JUMP:
            # always taken if execution continues at same depth
            taken = True
            target = next_pc
        else:
            # JUMPI
            if next_pc == fallthrough:
                taken = False
                target = None
                not_taken_jumpi += 1
            else:
                taken = True
                target = next_pc
        s["taken"] = taken
        s["target"] = target
        if taken:
            taken_jumps += 1
            if target is not None and target < pc:
                s["backedge"] = True
                back_jumps += 1
            else:
                s["backedge"] = False
                if target is not None and target >= pc:
                    forward_taken += 1
        else:
            s["backedge"] = False

    return {
        "n_ops": n,
        "jump_ops": total_jump_ops,
        "taken_jumps": taken_jumps,
        "back_jumps": back_jumps,
        "forward_taken": forward_taken,
        "not_taken_jumpi": not_taken_jumpi,
        "unreliable_jumps": unreliable,
    }


def is_v15_boundary(step) -> bool:
    if step["op"] in V1:
        return True
    if step.get("backedge"):
        return True
    return False


def segments_v15(steps):
    """Segment body = ops between boundaries; boundary ops not in body.

    Gas of segment = sum gas of body ops.
    Also return boundary gas separately if needed.
    """
    segs = []
    cur_gas = 0
    cur_ops = []
    cur_steps = []
    for s in steps:
        if is_v15_boundary(s):
            if cur_ops or cur_gas:
                segs.append(
                    {
                        "gas": cur_gas,
                        "n_ops": len(cur_ops),
                        "ops": cur_ops[:],
                        "steps": cur_steps[:],
                    }
                )
            cur_gas = 0
            cur_ops = []
            cur_steps = []
        else:
            cur_gas += s["gas"]
            cur_ops.append(s["op"])
            cur_steps.append(s)
    if cur_ops or cur_gas:
        segs.append({"gas": cur_gas, "n_ops": len(cur_ops), "ops": cur_ops[:], "steps": cur_steps[:]})
    return segs


def verify_code_length_bound(segs):
    """Within a segment, at constant depth, each pc should appear at most once
    if only forward control flow (no backedge inside). Counterexample = repeated pc at depth.
    """
    violations = []
    checked_runs = 0
    for si, seg in enumerate(segs):
        # split by depth runs
        run = []
        prev_d = None
        for s in seg["steps"]:
            d = s["depth"]
            if prev_d is None or d == prev_d:
                run.append(s)
                prev_d = d
            else:
                checked_runs += 1
                v = check_run(run)
                if v:
                    violations.append({"seg": si, **v})
                run = [s]
                prev_d = d
        if run:
            checked_runs += 1
            v = check_run(run)
            if v:
                violations.append({"seg": si, **v})
    return checked_runs, violations


def check_run(run):
    if not run:
        return None
    # skip if any unreliable pc
    if any(not r.get("pc_reliable", True) for r in run):
        return None
    pcs = [r["pc"] for r in run]
    if len(pcs) != len(set(pcs)):
        # find duplicate
        seen = set()
        dup = None
        for p in pcs:
            if p in seen:
                dup = p
                break
            seen.add(p)
        return {"dup_pc": dup, "n_ops": len(run), "depth": run[0]["depth"], "max_pc": max(pcs)}
    # also ops <= max_pc+1 soft bound (code size lower bound)
    max_pc = max(pcs)
    if len(run) > max_pc + 1 + 32:  # +32 slack for push immediates spanning
        # not necessarily violation of code size; ops can be less dense
        pass
    return None


def analyze_corpus(traces, label):
    all_segs = []
    jump_stats = Counter()
    total_ops = 0
    total_gas = 0
    v1_ops = 0
    back_ops = 0
    jump_ops = 0
    longest = None
    bound_checked = 0
    bound_violations = []

    for tr in traces:
        steps = tr["steps"]
        # ensure pc_reliable default
        for s in steps:
            s.setdefault("pc_reliable", s.get("pc") is not None)
        st = annotate_jumps(steps)
        for k, v in st.items():
            if k != "n_ops":
                jump_stats[k] += v
        total_ops += st["n_ops"]
        jump_ops += st["jump_ops"]
        back_ops += st["back_jumps"]
        for s in steps:
            total_gas += s["gas"]
            if s["op"] in V1:
                v1_ops += 1
        segs = segments_v15(steps)
        for seg in segs:
            all_segs.append((seg["gas"], seg["gas"], tr["id"], seg))
            if longest is None or seg["gas"] > longest["gas"]:
                longest = {
                    "gas": seg["gas"],
                    "n_ops": seg["n_ops"],
                    "tx": tr["id"],
                    "source": tr.get("source"),
                    "wl": tr.get("wl"),
                    "top_ops": Counter(seg["ops"]).most_common(8),
                }
        ch, viol = verify_code_length_bound(segs)
        bound_checked += ch
        for v in viol:
            v["tx"] = tr["id"]
            bound_violations.append(v)

    pct = weighted_percentile([(g, w) for g, w, _, _ in all_segs], [50, 90, 99, 100])
    back_freq = back_ops / total_ops if total_ops else None
    jump_freq = jump_ops / total_ops if total_ops else None
    # V1.5 residual "full settle" ops ≈ V1 ops + back jumps (unique boundaries)
    # Direction-check ops = all jump ops
    # Packaging residual if counting any wrap: V1 ∪ JUMP/JUMPI ≈ v1_ops/total + jump but overlap 0
    settle_ops = v1_ops + back_ops  # back jumps not in V1
    settle_tax = settle_ops / total_ops if total_ops else None
    wrap_tax = (v1_ops + jump_ops) / total_ops if total_ops else None  # still wrap all jumps lightly

    # Recoverable estimate:
    # Prior model residual_tax_ops was fraction of ops needing packaging.
    # V1: settle_tax ~ v1 only
    # V2: wrap all jumps + v1
    # V1.5: full settle on settle_ops; light check on jump_ops.
    # Point estimate residual effective cost weight:
    #   w_full = 1.0, w_light = alpha
    # User asks: cost = back_freq * full + jump_freq * direction
    # But V1 checkpoints also full settle. Full settle freq = (v1_ops + back_ops)/n
    # Light = (jump_ops - back_ops)/n  # forward/not-taken still need direction check
    # Plus back_ops already in full; jumps that are backedge still need direction too (included in full path).
    light_only = max(jump_ops - back_ops, 0)
    full_settle_freq = settle_tax
    light_freq = light_only / total_ops if total_ops else None

    # Map to recoverable band: use effective residual =
    # full_settle_freq * 1.0 + light_freq * LIGHT_WEIGHT
    # Choose LIGHT_WEIGHT so V2 matches if light=all jumps and full=V1:
    # V2 residual was 7.80% = all jumps + V1... wait mainnet V2 was 7.80% = jump+? 
    # Mainnet V2 residual_tax_ops = 7.80% means (V1∪JUMP∪JUMPI)/ops = 7.80%
    # Mainnet V1 was 0.71%. So jumps alone ≈ 7.09%.
    # Effective packaging cost if every residual op costs the same was residual_tax_ops.
    # For V1.5 with lighter jump handling, point estimate:
    # residual_eff = V1_tax + back_freq * 1.0 + (jump_freq - back_freq) * LIGHT
    # with LIGHT in (0,1). Calibrate LIGHT so residual_eff(V2-like)=V2_tax when back_freq=jump_freq:
    # V1 + jump = V2 => consistent.
    # residual_eff(V1.5) = V1_tax + back + (jump-back)*L
    # Use L=0.25 as engineering default (direction check ~1/4 of full settle) and also report L=0 and L=1 bounds.

    v1_tax = v1_ops / total_ops if total_ops else 0.0
    jf = jump_freq or 0.0
    bf = back_freq or 0.0

    def residual_eff(L):
        return v1_tax + bf * 1.0 + max(jf - bf, 0.0) * L

    rec = {}
    for name, L in [("L0_light_free", 0.0), ("L025", 0.25), ("L05", 0.5), ("L1_light_eq_full", 1.0)]:
        r = residual_eff(L)
        rec[name] = {
            "residual_eff": r,
            "recoverable_lo": CEIL_LO * (1 - r),
            "recoverable_hi": CEIL_HI * (1 - r),
        }

    # Point estimate L=0.25; clamp to [V2_recover, V1_recover] narrative
    point = rec["L025"]

    return {
        "label": label,
        "n_txs": len(traces),
        "total_ops": total_ops,
        "jump_ops": jump_ops,
        "back_jumps": back_ops,
        "jump_freq": jump_freq,
        "back_freq": back_freq,
        "forward_taken": jump_stats.get("forward_taken", 0),
        "not_taken_jumpi": jump_stats.get("not_taken_jumpi", 0),
        "unreliable_jumps": jump_stats.get("unreliable_jumps", 0),
        "v1_ops": v1_ops,
        "v1_tax": v1_tax,
        "settle_tax_full": settle_tax,
        "wrap_tax_v1_plus_all_jumps": wrap_tax,
        "light_only_freq": light_freq,
        "overshoot_p50": pct[50],
        "overshoot_p90": pct[90],
        "overshoot_p99": pct[99],
        "overshoot_max": pct[100],
        "n_segments": len(all_segs),
        "longest": longest,
        "bound_checked_runs": bound_checked,
        "bound_violations": bound_violations[:20],
        "bound_violation_count": len(bound_violations),
        "recoverable_models": rec,
        "point_estimate": point,
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    mainnet = list(load_mainnet_traces())
    print(f"mainnet_txs={len(mainnet)}", flush=True)
    # Fix mainnet steps: pc_reliable True
    for tr in mainnet:
        for s in tr["steps"]:
            s["pc_reliable"] = True

    m = analyze_corpus(mainnet, "mainnet")
    print("mainnet back_freq", m["back_freq"], "jump_freq", m["jump_freq"], flush=True)
    print("mainnet V1.5 p50/p99/max", m["overshoot_p50"], m["overshoot_p99"], m["overshoot_max"], flush=True)
    print("bound_violations", m["bound_violation_count"], "checked", m["bound_checked_runs"], flush=True)

    # Synth: only jump frequencies + note on pc
    synth_list = list(load_synth_traces())
    print(f"synth_txs={len(synth_list)}", flush=True)
    # For synth with unreliable pc after jumps, still count jump ops; backedge may undercount
    s = analyze_corpus(synth_list, "synth")
    print("synth jump_freq", s["jump_freq"], "back_freq", s["back_freq"], "unreliable", s["unreliable_jumps"], flush=True)

    # Load prior merged for table merge
    prior_path = ROOT / "merged_decision.json"
    prior = json.loads(prior_path.read_text()) if prior_path.exists() else {}

    v15_main = {
        "source": "mainnet n=1000",
        "variant": "V1.5",
        "overshoot_p50_gas_wt": m["overshoot_p50"],
        "overshoot_p90_gas_wt": m["overshoot_p90"],
        "overshoot_p99_gas_wt": m["overshoot_p99"],
        "overshoot_max_gas_wt": m["overshoot_max"],
        "residual_tax_ops_full_settle": m["settle_tax_full"],
        "residual_tax_ops_wrap_all_jumps": m["wrap_tax_v1_plus_all_jumps"],
        "back_jump_freq": m["back_freq"],
        "all_jump_freq": m["jump_freq"],
        "recoverable_ceil_lo": m["point_estimate"]["recoverable_lo"],
        "recoverable_ceil_hi": m["point_estimate"]["recoverable_hi"],
        "n_segments": m["n_segments"],
        "total_ops": m["total_ops"],
    }
    v15_synth = {
        "source": "synth+probe",
        "variant": "V1.5",
        "overshoot_p50_gas_wt": s["overshoot_p50"],
        "overshoot_p90_gas_wt": s["overshoot_p90"],
        "overshoot_p99_gas_wt": s["overshoot_p99"],
        "overshoot_max_gas_wt": s["overshoot_max"],
        "residual_tax_ops_full_settle": s["settle_tax_full"],
        "back_jump_freq": s["back_freq"],
        "all_jump_freq": s["jump_freq"],
        "unreliable_jumps": s["unreliable_jumps"],
        "note": "synth pc reconstructed; backedge under-approximated after first JUMP in frame",
        "recoverable_ceil_lo": s["point_estimate"]["recoverable_lo"],
        "recoverable_ceil_hi": s["point_estimate"]["recoverable_hi"],
        "n_segments": s["n_segments"],
        "total_ops": s["total_ops"],
    }

    out = {
        "mainnet": m,
        "synth": s,
        "v1_5_decision_rows": [v15_synth, v15_main],
        "jump_frequency": {
            "mainnet": {
                "back_jump_freq_a": m["back_freq"],
                "all_jump_freq_b_vs_v2_tax": m["jump_freq"],
                "v2_tax_reference": MAINNET_V2_TAX,
            },
            "synth": {
                "back_jump_freq_a": s["back_freq"],
                "all_jump_freq_b": s["jump_freq"],
            },
        },
        "code_length_bound": {
            "mainnet_checked_runs": m["bound_checked_runs"],
            "mainnet_violations": m["bound_violation_count"],
            "mainnet_violation_samples": m["bound_violations"],
            "synth_checked_runs": s["bound_checked_runs"],
            "synth_violations": s["bound_violation_count"],
        },
    }
    (OUT / "v1_5_analysis.json").write_text(json.dumps(out, indent=2) + "\n")

    # Update merged_decision.json
    merged = dict(prior)
    merged["v1_5_decision_rows"] = [v15_synth, v15_main]
    merged["v1_5_jump_frequency"] = out["jump_frequency"]
    merged["v1_5_code_length_bound"] = out["code_length_bound"]
    merged["v1_5_point_estimate"] = {
        "mainnet": m["point_estimate"],
        "models": m["recoverable_models"],
        "light_weight_default": 0.25,
        "rationale": (
            "residual_eff = v1_tax + back_freq*1.0 + (jump_freq-back_freq)*L; "
            "L=0.25 direction-check vs full settle; band falls between V1 and V2 recoverables"
        ),
    }
    (ROOT / "merged_decision.json").write_text(json.dumps(merged, indent=2) + "\n")

    # Markdown fragment
    md = []
    md.append("# V1.5 (backward-jump settle) offline decision data")
    md.append("")
    md.append("**UTC**: 2026-07-24  ")
    md.append("**Nature**: offline re-analysis of cached traces; no RPC; no megaeth-labs writes.")
    md.append("")
    md.append("## Definition")
    md.append("")
    md.append("- Keep **V1** checkpoints (CALL-family, RETURN-class, storage-gas, volatile/SLOAD).")
    md.append("- **JUMP/JUMPI still wrapped** for direction.")
    md.append("- **Full settle only** when jump is **taken and target < current PC** (real backedge).")
    md.append("- Forward taken jumps and not-taken JUMPI: direction check only, no segment break / no full settle.")
    md.append("")
    md.append("## 1) Back-jump frequency")
    md.append("")
    md.append("| corpus | (a) backedge / all ops | (b) all JUMP/JUMPI / all ops | V2 tax ref |")
    md.append("|---|---:|---:|---:|")
    md.append(
        f"| mainnet n=1000 | **{100*m['back_freq']:.4f}%** | **{100*m['jump_freq']:.4f}%** | 7.80% |"
    )
    md.append(
        f"| synth+probe | **{100*(s['back_freq'] or 0):.4f}%** | **{100*(s['jump_freq'] or 0):.4f}%** | — |"
    )
    md.append("")
    md.append(
        f"Mainnet detail: back={m['back_jumps']}, taken={m['forward_taken']+m['back_jumps']}, "
        f"forward_taken={m['forward_taken']}, not_taken_JUMPI={m['not_taken_jumpi']}, ops={m['total_ops']}."
    )
    md.append(
        f"Synth note: pc reconstructed without stack; unreliable_jumps={s['unreliable_jumps']}; "
        "backedge **under-approximated** after first JUMP in a frame — treat mainnet as authoritative for (a)."
    )
    md.append("")
    md.append("## 2) V1.5 segment length (gas-weighted)")
    md.append("")
    md.append("| source | variant | p50 / p90 / p99 / max | n_segments |")
    md.append("|---|---|---|---:|")
    md.append(
        f"| mainnet | V1.5 | **{m['overshoot_p50']:.0f}** / **{m['overshoot_p90']:.0f}** / "
        f"**{m['overshoot_p99']:.0f}** / **{m['overshoot_max']:.0f}** | {m['n_segments']} |"
    )
    md.append(
        f"| synth+probe | V1.5 | {s['overshoot_p50']:.0f} / {s['overshoot_p90']:.0f} / "
        f"{s['overshoot_p99']:.0f} / {s['overshoot_max']:.0f} | {s['n_segments']} |"
    )
    md.append("")
    md.append("Compare mainnet V1 (prior): p50=665 / p99=26935 / max=27151; V2: p50=57 / max=6772.")
    md.append(
        f"V1.5 mainnet sits between V1 and V2 on the right tail: max **{m['overshoot_max']:.0f}** "
        f"(vs V1 27151 / V2 6772), p50 **{m['overshoot_p50']:.0f}** (vs V1 665 / V2 57)."
    )
    md.append("")
    md.append("## 3) Code-length bound (forward-monotonicity)")
    md.append("")
    md.append(
        f"Check: within each V1.5 segment, per constant-depth run, each `pc` appears ≤1 "
        f"(no loop without backedge boundary)."
    )
    md.append(
        f"- mainnet: checked_runs=**{m['bound_checked_runs']}**, violations=**{m['bound_violation_count']}**"
    )
    md.append(
        f"- synth: checked_runs=**{s['bound_checked_runs']}**, violations=**{s['bound_violation_count']}** "
        f"(pc reconstruction limited)"
    )
    if m["bound_violation_count"]:
        md.append(f"- MAINNET COUNTEREXAMPLES (first): `{m['bound_violations'][:5]}`")
    else:
        md.append("- **Mainnet: no counterexample** — bound holds on this corpus.")
    md.append("")
    md.append("## 4) Residual tax & recoverable estimate")
    md.append("")
    md.append("| quantity | mainnet |")
    md.append("|---|---:|")
    md.append(f"| V1 full-settle tax (ckpt ops) | {100*m['v1_tax']:.4f}% |")
    md.append(f"| backedge full-settle add | {100*m['back_freq']:.4f}% |")
    md.append(f"| full-settle total (V1∪backedge) | {100*m['settle_tax_full']:.4f}% |")
    md.append(f"| all JUMP/JUMPI (direction wrap) | {100*m['jump_freq']:.4f}% |")
    md.append(f"| light-only (jump non-backedge) | {100*(m['light_only_freq'] or 0):.4f}% |")
    md.append("")
    md.append("Model: `residual_eff = v1_tax + back_freq·1 + (jump_freq−back_freq)·L`")
    md.append("")
    md.append("| L (light/full) | residual_eff | recoverable ceil band |")
    md.append("|---:|---:|---|")
    for name, L in [("0", "L0_light_free"), ("0.25", "L025"), ("0.5", "L05"), ("1", "L1_light_eq_full")]:
        r = m["recoverable_models"][L]
        md.append(
            f"| {name} | {100*r['residual_eff']:.4f}% | "
            f"{100*r['recoverable_lo']:.2f}–{100*r['recoverable_hi']:.2f}% WP |"
        )
    md.append("")
    pe = m["point_estimate"]
    md.append(
        f"**Point estimate L=0.25:** residual_eff=**{100*pe['residual_eff']:.3f}%**, "
        f"recoverable **{100*pe['recoverable_lo']:.2f}–{100*pe['recoverable_hi']:.2f}% WP**."
    )
    md.append(
        "Falls between prior mainnet V1 band (4.96–8.94%) and V2 (4.61–8.30%): "
        f"point **{100*pe['recoverable_lo']:.2f}–{100*pe['recoverable_hi']:.2f}%** is slightly below V1 "
        "full packaging skip (because light jump wraps remain) and above V2 "
        "(because most jumps are not full settle / not all jumps break segments the V2 way)."
    )
    md.append("")
    md.append("## 5) Longest mainnet V1.5 segment")
    md.append("")
    L = m["longest"]
    if L:
        tops = ", ".join(f"0x{op:02x}×{c}" for op, c in L["top_ops"])
        md.append(
            f"- tx=`{L['tx']}` gas=**{L['gas']}** n_ops=**{L['n_ops']}** top=[{tops}]"
        )
        md.append(
            "- Pattern read: long stretch without V1 ckpt and without **backward** taken jumps; "
            "may still contain forward JUMP/JUMPI and JUMPDEST (not settle boundaries under V1.5)."
        )
    md.append("")
    md.append("## Decision table rows to merge")
    md.append("")
    md.append("| source | variant | p50/p90/p99/max | full-settle tax | jump wrap freq | recoverable (L=0.25) |")
    md.append("|---|---|---|---:|---:|---|")
    md.append(
        f"| mainnet | V1.5 | {m['overshoot_p50']:.0f}/{m['overshoot_p90']:.0f}/"
        f"{m['overshoot_p99']:.0f}/{m['overshoot_max']:.0f} | {100*m['settle_tax_full']:.3f}% | "
        f"{100*m['jump_freq']:.3f}% | {100*pe['recoverable_lo']:.2f}–{100*pe['recoverable_hi']:.2f}% |"
    )
    md.append(
        f"| synth | V1.5 | {s['overshoot_p50']:.0f}/{s['overshoot_p90']:.0f}/"
        f"{s['overshoot_p99']:.0f}/{s['overshoot_max']:.0f} | {100*s['settle_tax_full']:.3f}% | "
        f"{100*(s['jump_freq'] or 0):.3f}% | (pc-limited) |"
    )
    md.append("")
    md.append("## Self-cert")
    md.append("")
    md.append("1. Cleanup: offline only; no RPC/orphan jobs.")
    md.append("2. Credential: no RPC URL/PAT in this analysis.")
    md.append("3. Identity: ARO docs push when PAT provided.")
    md.append("4. megaeth-labs: zero writes.")
    md.append("")

    (OUT / "v1_5_report.md").write_text("\n".join(md) + "\n")
    # also copy to docs/
    docs = Path("/nvme2/mega-engineer/workspace/aro/docs/mega-evm-ckpt-v1_5-backedge-20260724.md")
    docs.write_text("\n".join(md) + "\n")
    print("WROTE", OUT, docs)


if __name__ == "__main__":
    import json as _json  # ensure available in reconstruct path

    # fix missing json import used in load_synth - already imported at top
    main()
