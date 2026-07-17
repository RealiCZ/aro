"""T50: `_inst_crate` length-prefix parse + generator transcript persistence.

Hermetic — pure string checks for symbols; temp dirs + mocked write for
transcripts. No cargo, no model, no network.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from aro.events import EventLog
from aro.generator import persist_agent_transcript
from aro.symbols import _fn_name, _inst_crate, _length_prefixed_frags, classify_owner
from aro.types import GenContext, Metrics, Objective


# Salt profile hotspot (operator-verified misparse under the old greedy regex).
_SALT_SYM = (
    "_RNvNtCscZb8DOAJ9o2_11banderwagon14salt_committer16add_affine_point"
)
# mega-evm-style symbol with a genuine trailing monomorphization-instantiation crate.
_MEGA_SYM = "_RNvNtCsAA_8mega_evm3evm7executeCsBB_16sweep_hotloop_v2"


def case_66():
    print("=== case 66: _inst_crate length-prefix + agent transcripts ===")

    # --- (1) _inst_crate: four core cases ------------------------------------
    # Underscore trailing crate (the original motivating example) still works.
    assert _inst_crate("Cs1234_16sweep_hotloop_v2") == "sweep_hotloop_v2"
    assert _inst_crate(_MEGA_SYM) == "sweep_hotloop_v2"

    # Salt path-root crate mid-symbol must NOT match (greedy [a-z0-9_]*$ used to
    # swallow `banderwagon14salt_committer16add_affine_point`).
    assert _inst_crate(_SALT_SYM) is None, _inst_crate(_SALT_SYM)

    # Malformed length (runs past end of symbol) → None.
    assert _inst_crate("CsXX_99too_short") is None
    assert _inst_crate("CsAA_5ab") is None  # wants 5 chars, only 2 remain

    # No Cs marker at all → None.
    assert _inst_crate("plain_function_name") is None
    assert _inst_crate("") is None
    assert _inst_crate(None) is None  # type: ignore[arg-type]

    # Composition with length-prefixed frags + frags[-1]==inst trim.
    frags = _length_prefixed_frags(_SALT_SYM)
    assert "banderwagon" in frags and "add_affine_point" in frags, frags
    assert _length_prefixed_frags(_MEGA_SYM)[-1] == "sweep_hotloop_v2"
    print("#66a OK: _inst_crate length-prefix (underscore / salt mid / malformed / none)")

    # --- (2) Owner classification end-to-end ---------------------------------
    # Salt symbol with fixture token list → owner resolves via banderwagon.
    owner, why = classify_owner(_SALT_SYM, {"banderwagon", "salt"})
    assert owner == "ours" and why == "banderwagon", (owner, why)
    # Single-token form too.
    assert classify_owner(_SALT_SYM, "banderwagon") == ("ours", "banderwagon")

    # mega-evm regression: genuine trailing inst crate is excluded so an external
    # monomorphized op does not inherit the probe binary as "ours".
    # `_MEGA_SYM` has mega_evm in the defining path → still ours via that token,
    # and the trailing sweep_hotloop_v2 is stripped (not the owner label).
    assert classify_owner(_MEGA_SYM, "mega_evm") == ("ours", "mega_evm")
    # External fn monomorphized into our probe binary: inst crate stripped so we
    # do not mis-label as ours via the probe name alone.
    ark = ("_RNvNtCsZZ_7ark_ff3mulCsBB_16sweep_hotloop_v2")
    assert _inst_crate(ark) == "sweep_hotloop_v2"
    assert classify_owner(ark, "mega_evm")[0] != "ours"
    # Leaf name keeps the real function, not the binary crate.
    assert _fn_name(_MEGA_SYM, "mega_evm", "sweep_hotloop_v2") == "execute"
    assert _fn_name(_SALT_SYM, "banderwagon") == "add_affine_point"
    print("#66b OK: salt owner=banderwagon; trailing inst crate still excluded")

    # --- (3) Transcript persistence ------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ev = EventLog(td / "events.jsonl", also_console=False)
        ev.context = {"attempt": 3}
        ctx = GenContext(
            round=1,
            objectives=[Objective(metric="ns", minimize=True)],
            baseline=Metrics(),
            memory_summary="(first round)",
            emit=ev.emit,
            out_dir=td,
        )
        path = persist_agent_transcript(
            ctx, k=0, prompt="PROMPT BODY", reply="REPLY BODY\n@@FILE@@ src/a.rs\n",
            files_changed=["src/a.rs", "README.md"], file_blocks=1,
            reason="agent made no usable .rs edits", edits_n=0)
        assert path is not None and Path(path).is_file(), path
        assert "agent-transcripts" in path
        assert "attempt-3-round-1-k0.md" in path
        body = Path(path).read_text()
        assert "## Prompt" in body and "PROMPT BODY" in body
        assert "## Reply" in body and "REPLY BODY" in body
        assert "## Verdict" in body
        assert "agent made no usable .rs edits" in body
        assert "src/a.rs" in body and "@@FILE@@ blocks: 1" in body
        assert "usable .rs edits: 0" in body

        lines = (td / "events.jsonl").read_text().strip().splitlines()
        assert lines, "expected generator_transcript event"
        rec = json.loads(lines[-1])
        assert rec["event"] == "generator_transcript"
        assert rec["path"] == path
        assert rec["reason"] == "agent made no usable .rs edits"
        assert rec["files"] == ["src/a.rs", "README.md"]
        assert rec["file_blocks"] == 1 and rec["edits_n"] == 0
        assert rec.get("attempt") == 3  # ambient EventLog.context stamp

    # Write failure → warn, attempt continues (returns None, no raise).
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # out_dir is a FILE, so creating agent-transcripts/ under it fails.
        bad = td / "not-a-dir"
        bad.write_text("x")
        ctx = GenContext(
            round=0, objectives=[], baseline=Metrics(), memory_summary="",
            out_dir=bad)
        got = persist_agent_transcript(ctx, k=0, prompt="p", reply="r", reason="x")
        assert got is None

    # No out_dir and no EventLog → silent no-op (returns None).
    bare = GenContext(round=0, objectives=[], baseline=Metrics(), memory_summary="")
    assert persist_agent_transcript(bare, k=0, prompt="p", reply="r") is None
    print("#66c OK: transcript written with sections; write fail warns; events carry path")
    print("case_66 OK: inst-crate + agent transcripts")
