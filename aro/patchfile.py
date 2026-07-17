"""patchfile — the single owner of ARO's on-disk patch format (`patches/<id>.txt`).

One candidate's patch is dumped as SEARCH/REPLACE blocks (or the literal `NoOp`):

    --- edit 1 ---
    path: <repo-relative file>
    <<<<<<< SEARCH
    <exact current content>
    =======
    <replacement>
    >>>>>>> REPLACE

`dump` and `parse` round-trip byte-exactly for any edit whose search/replace do not
themselves contain a bare sentinel line — the same invariant the previous copies in
store.py and verify_patch.py relied on. Everything that reads or writes this format
(store, manifest, tree) goes through here, so a format change is one
edit, not four.
"""
from __future__ import annotations

from .types import Edit, Patch

SEARCH_MARK = "<<<<<<< SEARCH"
SEP_MARK = "======="
REPLACE_MARK = ">>>>>>> REPLACE"


def safe_id(cid: str) -> str:
    """Candidate id → a filesystem-safe patch filename stem."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in cid)


def dump(patch: Patch) -> str:
    """Serialize a Patch to the patches/<id>.txt text (trailing newline included)."""
    if patch.is_noop:
        return "NoOp\n"
    out = []
    for i, e in enumerate(patch.edits, 1):
        out.append(f"--- edit {i} ---")
        out.append(f"path: {e.path}")
        out.append(SEARCH_MARK)
        out.append(e.search)
        out.append(SEP_MARK)
        out.append(e.replace)
        out.append(REPLACE_MARK)
    return "\n".join(out) + "\n"


def parse(text: str) -> list:
    """Parse a patches/<id>.txt dump (NoOp or SEARCH/REPLACE blocks) into [Edit]."""
    lines = text.split("\n")
    edits, i = [], 0
    while i < len(lines):
        if lines[i].startswith("path: "):
            path = lines[i][len("path: "):]
            i += 1
            if i < len(lines) and lines[i] == SEARCH_MARK:
                i += 1
                search = []
                while i < len(lines) and lines[i] != SEP_MARK:
                    search.append(lines[i]); i += 1
                i += 1
                replace = []
                while i < len(lines) and lines[i] != REPLACE_MARK:
                    replace.append(lines[i]); i += 1
                edits.append(Edit(path, "\n".join(search), "\n".join(replace)))
        i += 1
    return edits
