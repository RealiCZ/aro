"""CodeContextProvider (grep/regex MVP).

A multi-site optimization needs the generator to see the hot function *together
with* the data structure it reads and the code that builds that structure — not
just the function in isolation. Given a source file and a
few anchor symbols, this extracts their definitions (brace-matched, or
`;`-terminated for const/static) and assembles them into a context block for the
prompt. No LSP; heuristic text extraction (brace counting ignores braces inside
strings/comments — fine for the well-formed functions we target). All matching
definitions are returned, so cfg-gated variants are both shown.
"""
from __future__ import annotations

import re
from pathlib import Path


def _blocks(source: str, kind: str, name: str, max_lines: int):
    pat = re.compile(
        rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?{kind}\s+{re.escape(name)}\b")
    lines = source.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not pat.match(line):
            continue
        if kind in ("const", "static"):
            buf = []
            j = i
            while j < len(lines):
                buf.append(lines[j])
                if ";" in lines[j]:
                    break
                j += 1
            out.append("\n".join(buf))
            continue
        depth, started, buf = 0, False, []
        for j in range(i, min(len(lines), i + max_lines * 4)):
            buf.append(lines[j])
            depth += lines[j].count("{") - lines[j].count("}")
            if "{" in lines[j]:
                started = True
            if started and depth <= 0:
                break
        if len(buf) > max_lines:
            buf = buf[:max_lines] + ["    // ... (truncated)"]
        out.append("\n".join(buf))
    return out


def extract(file_path, anchors, max_lines: int = 90) -> str:
    """`anchors` = list of (kind, name), kind in {fn, struct, const, static, impl}.
    Returns a labeled, concatenated code block (empty string on read failure)."""
    try:
        source = Path(file_path).read_text()
    except Exception:
        return ""
    name = Path(file_path).name
    parts = []
    for kind, sym in anchors:
        for block in _blocks(source, kind, sym, max_lines):
            parts.append(f"// --- {kind} {sym}  (from {name}) ---\n{block}")
    return "\n\n".join(parts)
