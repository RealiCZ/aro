"""llm — the single agentic CLI invocation point.

Every generation-side model call in ARO (ralph / agentic generation, read + reflect,
semantic critic, probe/workload factories, and plan agent) goes through `run_llm`.
Backend command construction and reply parsing vary, while timeout policy, failure
surfacing, and token accounting remain centralized. `run_claude` is the compatibility
alias pinned to the default Claude adapter.

Failure policy: `run_llm` RAISES `LLMError` (spawn failure, timeout, non-zero exit,
or malformed structured output). Callers decide what that means — generators emit a
traceable `generator_error`, the critic default-rejects, and the plan CLI aborts.

Model-tier fallback: quota/overload exhaustion on the primary model (the
generator-down fuse in attempt.py exists precisely because this used to kill
a whole campaign) is safe to degrade through rather than hard-stop on — every
candidate a generator proposes, at any model tier, still has to clear the
SAME deterministic judge (byte-identical + regression + significance). A
weaker fallback model producing worse patches just means more rejections, not
a corrupted result. For Claude, `--fallback-model` is the CLI's own primitive for
this (model-unavailable, not our own retry loop); the adapter always passes a chain.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

# Overridable for environments where the CLI is not on PATH under this name.
CLAUDE_BIN = os.environ.get("ARO_CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("ARO_CODEX_BIN", "codex")
GROK_BIN = os.environ.get("ARO_GROK_BIN", "grok")

# Comma-separated fallback chain passed straight through to the CLI's own
# --fallback-model (tried in order when the primary is overloaded/exhausted,
# e.g. a weekly quota wall); empty string disables fallback entirely.
CLAUDE_FALLBACK_MODELS = os.environ.get("ARO_CLAUDE_FALLBACK_MODELS", "sonnet")

# Grok documents turns as model/tool-loop rounds, not seconds, and publishes no
# default. Keep a high adapter cap independent of the subprocess wall-clock timeout.
GROK_MAX_TURNS = 100

# Grok's built-in sandbox profiles warn and continue unsandboxed when kernel
# enforcement is unavailable. Explicit custom profiles fail closed instead. Hosts
# provision these two profiles as documented in docs/OPERATIONS.md.
GROK_READ_ONLY_SANDBOX = "aro-read-only"
GROK_WORKSPACE_SANDBOX = "aro-workspace"

Reply = Tuple[str, Optional[int], Optional[float]]


class LLMError(RuntimeError):
    """An LLM backend failed (launch / timeout / non-zero / malformed reply)."""


def _raise_for_status(name: str, stdout: str, stderr: str, returncode: int) -> None:
    if returncode != 0:
        tail = (stderr or stdout or "").strip()[-400:]
        raise LLMError(f"{name} exited {returncode}: {tail}")


class Backend(Protocol):
    """One agentic CLI transport used by the generation side of ARO."""

    name: str

    def build_cmd(self, prompt: str, cwd, allow_write: bool,
                  timeout_s: int) -> list[str]:
        ...

    def parse_reply(self, stdout: str, stderr: str, returncode: int) -> Reply:
        ...


@dataclass(frozen=True)
class ClaudeBackend:
    name: str = "claude"

    def build_cmd(self, prompt: str, cwd, allow_write: bool,
                  timeout_s: int) -> list[str]:
        cmd = [CLAUDE_BIN]
        if allow_write:
            cmd.append("--dangerously-skip-permissions")
        if CLAUDE_FALLBACK_MODELS:
            cmd += ["--fallback-model", CLAUDE_FALLBACK_MODELS]
        cmd += ["--output-format", "json", "-p", prompt]
        return cmd

    def parse_reply(self, stdout: str, stderr: str, returncode: int) -> Reply:
        _raise_for_status(self.name, stdout, stderr, returncode)
        try:
            obj = json.loads(stdout)
            if not isinstance(obj, dict) or not isinstance(obj.get("result"), str):
                raise ValueError("missing string result")
            raw_usage = obj.get("usage")
            usage = {} if raw_usage is None else raw_usage
            if not isinstance(usage, dict):
                raise ValueError("usage is not an object")
            tokens = int(usage.get("output_tokens", 0) or 0)
            cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"claude returned malformed JSON: {e}") from e
        return obj["result"], tokens, cost


@dataclass(frozen=True)
class CodexBackend:
    name: str = "codex"

    def build_cmd(self, prompt: str, cwd, allow_write: bool,
                  timeout_s: int) -> list[str]:
        workdir = str(cwd) if cwd else str(Path.cwd())
        mode = "workspace-write" if allow_write else "read-only"
        # `--json` emits JSONL on stdout, including the final agent message and
        # usage. `--output-last-message` writes out-of-band to a file, which does
        # not fit parse_reply(stdout, stderr, returncode); output-schema constrains
        # response content rather than transporting it.
        return [CODEX_BIN, "exec", "-C", workdir, "--sandbox", mode,
                "--json", prompt]

    def parse_reply(self, stdout: str, stderr: str, returncode: int) -> Reply:
        _raise_for_status(self.name, stdout, stderr, returncode)
        text = None
        tokens = None
        try:
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
                kind = event.get("type")
                if kind in ("error", "turn.failed"):
                    detail = event.get("message") or event.get("error") or event
                    raise LLMError(f"codex reply reported {kind}: {detail}")
                raw_item = event.get("item")
                item = {} if raw_item is None else raw_item
                if not isinstance(item, dict):
                    raise ValueError("item is not an object")
                if (kind == "item.completed" and item.get("type") == "agent_message"
                        and isinstance(item.get("text"), str)):
                    text = item["text"]
                if kind == "turn.completed":
                    raw_usage = event.get("usage")
                    usage = {} if raw_usage is None else raw_usage
                    if not isinstance(usage, dict):
                        raise ValueError("usage is not an object")
                    raw_tokens = usage.get("output_tokens")
                    tokens = int(raw_tokens) if raw_tokens is not None else None
        except LLMError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"codex returned malformed JSONL: {e}") from e
        if text is None:
            raise LLMError("codex returned JSONL without a completed agent message")
        return text, tokens, None


@dataclass(frozen=True)
class GrokBackend:
    name: str = "grok"

    def build_cmd(self, prompt: str, cwd, allow_write: bool,
                  timeout_s: int) -> list[str]:
        workdir = str(cwd) if cwd else str(Path.cwd())
        mode = GROK_WORKSPACE_SANDBOX if allow_write else GROK_READ_ONLY_SANDBOX
        cmd = [GROK_BIN, "-p", prompt, "--cwd", workdir,
               "--output-format", "json", "--max-turns", str(GROK_MAX_TURNS),
               "--sandbox", mode]
        # Headless Grok cancels tool calls that would otherwise prompt. Writable
        # generators need unattended edit/build approval; the OS workspace sandbox
        # remains the boundary. Read-only calls deliberately keep deny-by-default.
        if allow_write:
            cmd.append("--always-approve")
        return cmd

    def parse_reply(self, stdout: str, stderr: str, returncode: int) -> Reply:
        _raise_for_status(self.name, stdout, stderr, returncode)
        try:
            obj = json.loads(stdout)
            if not isinstance(obj, dict) or not isinstance(obj.get("text"), str):
                raise ValueError("missing string text")
            raw_usage = obj.get("usage")
            usage = {} if raw_usage is None else raw_usage
            if not isinstance(usage, dict):
                raise ValueError("usage is not an object")
            raw_tokens = usage.get("output_tokens")
            tokens = int(raw_tokens) if raw_tokens is not None else None
            raw_cost = obj.get("total_cost_usd")
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"grok returned malformed JSON: {e}") from e
        return obj["text"], tokens, cost


_BACKENDS = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
    "grok": GrokBackend(),
}


def get_backend(name: str) -> Backend:
    """Resolve an exact backend name, listing the supported names on error."""
    key = (name or "").strip().lower()
    try:
        return _BACKENDS[key]
    except KeyError as e:
        known = ", ".join(sorted(_BACKENDS))
        raise LLMError(f"unknown LLM backend {name!r}; known backends: {known}") from e


def select_backend(spec=None, *, critic: bool = False) -> Backend:
    """Select a backend for a target spec.

    Generators follow ARO_LLM_BACKEND > spec.llm_backend > claude. An explicit
    critic_backend is a deliberate cross-model topology and therefore selects
    the critic directly; when absent, the critic follows the generator choice.
    """
    critic_name = getattr(spec, "critic_backend", None) if spec is not None else None
    if critic and critic_name:
        return get_backend(critic_name)
    spec_name = getattr(spec, "llm_backend", None) if spec is not None else None
    name = os.environ.get("ARO_LLM_BACKEND") or spec_name or "claude"
    return get_backend(name)


def parse_json_reply(stdout: str) -> Reply:
    """Legacy helper: parse Claude JSON, falling back to raw plain text.

    Backend.parse_reply is intentionally strict for new structured transports;
    this standalone helper keeps its historical token-unaware fallback for callers
    that used it directly before the backend abstraction.
    """
    try:
        return _BACKENDS["claude"].parse_reply(stdout, "", 0)
    except LLMError:
        return stdout, 0, 0.0


def _plain_output_cmd(backend: Backend, cmd: list[str]) -> list[str]:
    """Remove the structured-output flag for the legacy json_output=False mode."""
    cmd = list(cmd)
    if backend.name == "claude":
        i = cmd.index("--output-format")
        del cmd[i:i + 2]
    elif backend.name == "codex":
        cmd.remove("--json")
    elif backend.name == "grok":
        i = cmd.index("--output-format")
        cmd[i + 1] = "plain"
    return cmd


def _grok_sandbox_degraded(stderr: str) -> bool:
    """Recognize Grok's fail-open warning on hosts without sandbox support."""
    message = (stderr or "").lower()
    if "sandbox" not in message:
        return False
    return any(marker in message for marker in (
        "continuing without enforcement", "continuing without sandbox",
        "defaulting to no sandbox", "could not be applied", "could not apply",
        "failed to apply", "sandbox unavailable",
    ))


def run_llm(prompt: str, *, backend=None, cwd=None, timeout: int = 600,
            allow_write: bool = False, env=None, json_output: bool = True) -> Reply:
    """Run one configured agentic CLI call → (text, output_tokens, cost_usd).

    The subprocess timeout and failure policy are shared across backends. `backend`
    may be a registered name or a Backend object; None applies ARO_LLM_BACKEND and
    then defaults to Claude. json_output=False retains run_claude's legacy raw mode.
    """
    if backend is None:
        selected = select_backend()
    elif isinstance(backend, str):
        selected = get_backend(backend)
    else:
        selected = backend
    cmd = selected.build_cmd(prompt, cwd, allow_write, timeout)
    if not json_output:
        cmd = _plain_output_cmd(selected, cmd)
    try:
        out = subprocess.run(cmd, cwd=(str(cwd) if cwd else None), env=env,
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"{selected.name} timed out after {timeout}s") from e
    except Exception as e:
        raise LLMError(f"{selected.name} failed to launch: {e}") from e
    _raise_for_status(selected.name, out.stdout, out.stderr, out.returncode)
    if selected.name == "grok" and _grok_sandbox_degraded(out.stderr):
        tail = (out.stderr or "").strip()[-400:]
        tier = "workspace" if allow_write else "read-only"
        raise LLMError(f"grok could not enforce its {tier} sandbox: {tail}")
    if json_output:
        return selected.parse_reply(out.stdout, out.stderr, out.returncode)
    return out.stdout, 0, 0.0


def run_claude(prompt: str, *, cwd=None, timeout: int = 600, allow_write: bool = False,
               env=None, json_output: bool = True) -> Reply:
    """Backward-compatible alias pinned to the Claude backend.

    allow_write=False (default) runs bare `claude` — default permissions block
    writes, so the model can only READ and answer (maker-checker: patches are
    applied later by the judge). allow_write=True passes
    --dangerously-skip-permissions for the agentic write-compile-fix loop, which
    must only ever run inside a throwaway worktree."""
    return run_llm(prompt, backend="claude", cwd=cwd, timeout=timeout,
                   allow_write=allow_write, env=env, json_output=json_output)
