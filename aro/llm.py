"""llm — the single `claude` CLI invocation point.

Every model call in ARO (ralph / agentic generation, the read + reflect phases, the
semantic critic, the plan agent) goes through `run_claude`, so timeout policy, the
binary name, error surfacing and token accounting live in exactly one place. Before
this module the invocation was copy-pasted six times with drifting timeouts and
silent `except: return None` failure paths — a systematically-broken generator was
indistinguishable from "the model proposed nothing".

Failure policy: `run_claude` RAISES `LLMError` (spawn failure, timeout, non-zero
exit). Callers decide what that means — generators catch it and emit a
`generator_error` event (traceable in events.jsonl), the critic lets its
default-reject handle it, the plan CLI aborts with the tail.
"""
from __future__ import annotations

import json
import os
import subprocess

# Overridable for environments where the CLI is not on PATH under this name.
CLAUDE_BIN = os.environ.get("ARO_CLAUDE_BIN", "claude")


class LLMError(RuntimeError):
    """A claude invocation failed (launch / timeout / non-zero exit)."""


def parse_json_reply(stdout: str):
    """Parse a `claude --output-format json` reply → (result_text, output_tokens,
    cost_usd). Falls back to (raw_stdout, 0, 0.0) when the output isn't that JSON
    (older CLI / plain text), so a token-unaware CLI degrades to "no token data"."""
    try:
        d = json.loads(stdout)
    except Exception:
        return (stdout, 0, 0.0)
    u = d.get("usage") or {}
    return (d.get("result", "") or "",
            int(u.get("output_tokens", 0) or 0),
            float(d.get("total_cost_usd", 0.0) or 0.0))


def run_claude(prompt: str, *, cwd=None, timeout: int = 600, allow_write: bool = False,
               env=None, json_output: bool = True):
    """One claude call → (text, output_tokens, cost_usd).

    allow_write=False (default) runs bare `claude` — default permissions block
    writes, so the model can only READ and answer (maker-checker: patches are
    applied later by the judge). allow_write=True passes
    --dangerously-skip-permissions for the agentic write-compile-fix loop, which
    must only ever run inside a throwaway worktree."""
    cmd = [CLAUDE_BIN]
    if allow_write:
        cmd.append("--dangerously-skip-permissions")
    if json_output:
        cmd += ["--output-format", "json"]
    cmd += ["-p", prompt]
    try:
        out = subprocess.run(cmd, cwd=(str(cwd) if cwd else None), env=env,
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"claude timed out after {timeout}s") from e
    except Exception as e:
        raise LLMError(f"claude failed to launch: {e}") from e
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip()[-400:]
        raise LLMError(f"claude exited {out.returncode}: {tail}")
    if json_output:
        return parse_json_reply(out.stdout)
    return (out.stdout, 0, 0.0)
