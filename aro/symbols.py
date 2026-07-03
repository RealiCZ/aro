"""symbols — Rust symbol naming: v0 demangling + owner classification.

Pure string machinery extracted from sweep.py: turn a (v0-mangled) symbol into a
readable leaf function name, and classify a frame's OWNER (ours / crypto / runtime /
unknown) so the frontier can say what is a lever vs untouchable. No subprocesses
except the optional `rustfilt` fallback; unit-testable without cargo.
"""
from __future__ import annotations

import re
import shutil

from . import profile as profmod

# Symbol markers. "Ours" is decided per-spec (the target crate's name). These tag the
# rest so the report can say WHY a heavy frame is not our lever.
_CRYPTO = ("keccak", "sha3", "p1600", "blake", "secp", "k256", "bn254", "bls12",
           "sha256", "ripemd", "modexp")
_RUNTIME = ("revm", "alloy", "op_revm", "op_alloy", "hashbrown", "foldhash", "ruint",
            "ark_", "num_bigint", "raw_vec", "hashmap", "btree", "core", "alloc", "std")


def _crate_token(pkg: str) -> str:
    """`mega-evm` package → the `mega_evm` token that appears in its mangled symbols."""
    return (pkg or "").replace("-", "_")


_NAME_NOISE = set(_RUNTIME) | set(_CRYPTO) | {
    "evm", "limit", "instructions", "host", "contract", "control", "interpreter",
    "context", "journal", "external", "primitives", "bits", "stack", "memory",
    "inner", "info", "state", "frame", "tx", "result", "spec", "types", "ext"}


def _inst_crate(symbol: str):
    """The trailing MONOMORPHIZATION-INSTANTIATION crate of a v0 symbol, recorded as
    `…Cs<base62>_<len><cratename>` at the very end (e.g. the probe/example binary,
    `sweep_hotloop`). It is NOT the function — a generic fn like `inspect_storage`
    carries it as a suffix, so picking the trailing fragment mislabels every
    monomorphized lever as the binary crate. Return that cratename so it is excluded."""
    m = re.search(r"Cs[0-9A-Za-z]+_\d+([a-z][a-z0-9_]*)$", symbol)
    return m.group(1) if m else None


def _fn_name(symbol: str, our_token: str, binary: str = "") -> str:
    """Readable leaf function name from a (v0-mangled) symbol. We scan the
    length-prefixed identifiers and return the LAST snake_case fragment that is not a
    crate / module / generic-arg token NOR the trailing instantiation crate — which is
    reliably the function name (`inspect_storage`, `check_limit`, `sstore`, `sload`, …).
    Stripping the instantiation crate is essential: without it a generic in-crate lever
    (`…inspect_storage Cs…_13sweep_hotloop`) is mislabeled as the binary and collapsed
    into one un-locatable frame — the bug that hid the real levers from the explorer."""
    frags, i = [], 0
    while i < len(symbol):
        if symbol[i].isdigit():
            j = i
            while j < len(symbol) and symbol[j].isdigit():
                j += 1
            n = int(symbol[i:j])
            frag = symbol[j:j + n]
            i = j + n
            if frag and (frag[0].isalpha() or frag[0] == "_"):
                frags.append(frag)
        else:
            i += 1
    inst = _inst_crate(symbol)
    ours = {our_token} if isinstance(our_token, str) else set(our_token or [])
    excl = _NAME_NOISE | ours | {binary} | ({inst} if inst else set())
    cand = [f for f in frags
            if re.match(r"^[a-z][a-z0-9_]*$", f) and f not in excl]
    if cand:
        return cand[-1]
    return profmod.demangle(symbol)


def _have_rustfilt():
    """Cached `rustfilt` path (the canonical rustc-demangle CLI), or None — then the
    in-house `_fn_name` heuristic is the fallback (zero hard dependency)."""
    c = _have_rustfilt.__dict__
    if "v" not in c:
        c["v"] = shutil.which("rustfilt")
    return c["v"]


def _split_top(s: str) -> list:
    """Split a demangled path on `::` at angle-bracket depth 0 (so `::` inside generic
    args `<…>` doesn't split)."""
    parts, depth, last, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c == "<":
            depth += 1
        elif c == ">":
            depth = max(0, depth - 1)
        elif c == ":" and depth == 0 and i + 1 < len(s) and s[i + 1] == ":":
            parts.append(s[last:i])
            i += 2
            last = i
            continue
        i += 1
    parts.append(s[last:])
    return parts


def _demangle_leaf(demangled: str) -> str:
    """Function-leaf name from a rustfilt-demangled path. The function name is the last
    top-level `::` segment that is a plain identifier (not a `<…>` Self-type or trailing
    turbofish): `<Journal<…> as …Tr>::inspect_storage`→inspect_storage,
    `…host::inspect_account::<…>`→inspect_account, `foldhash::hash_bytes_long`→that."""
    for p in reversed(_split_top(demangled)):
        p = p.strip()
        if p and not p.startswith("<"):
            return p
    return demangled.strip()


def _demangle_names(symbols: list, our_token: str, binary: str) -> list:
    """Each raw v0 symbol → its function-leaf name. rustfilt (correct v0 parse: fn name
    vs its generic args) when present; the heuristic otherwise. Owner is still decided
    on the RAW symbol, so a trait method `<revm::Journal as mega_evm::Tr>::inspect_storage`
    stays OURS even though its demangled head is the revm Self-type."""
    rf = _have_rustfilt()
    if rf and symbols:
        try:
            import subprocess
            out = subprocess.run([rf], input="\n".join(symbols), capture_output=True,
                                 text=True, timeout=30)
            lines = out.stdout.splitlines()
            if out.returncode == 0 and len(lines) == len(symbols):
                return [_demangle_leaf(l) for l in lines]
        except Exception:
            pass
    return [_fn_name(s, our_token, binary) for s in symbols]


def classify_owner(symbol: str, ours):
    """(owner, why) for a (possibly mangled) symbol. owner ∈ {ours, crypto, runtime,
    unknown}. `ours` may be a single crate token (str) or the whole workspace's token
    SET — a symbol is OURS if ANY token appears in it (longest first, so a specific crate
    wins over a short one). In-crate fns are generic over external types, so a plain
    substring is enough."""
    s = symbol.lower()
    # Strip the trailing MONOMORPHIZATION-INSTANTIATION crate before the ownership check:
    # an EXTERNAL fn (e.g. arkworks `Fr::mul_assign`) monomorphized inside a workspace
    # crate carries that crate as a `Cs…_<crate>` suffix — without stripping it, every
    # arkworks op called from our code would be mis-classified `ours`.
    inst = _inst_crate(symbol)
    s_check = s.rsplit(inst.lower(), 1)[0] if inst else s
    toks = {ours} if isinstance(ours, str) else set(ours or [])
    hit = next((t for t in sorted(toks, key=len, reverse=True) if t and t in s_check), None)
    if hit:
        return "ours", hit
    for m in _CRYPTO:
        if m in s:
            return "crypto", m
    for m in _RUNTIME:
        if m in s:
            return "runtime", m
    return "unknown", ""
