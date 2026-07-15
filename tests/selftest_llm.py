from __future__ import annotations

import tempfile
import types as _types
from pathlib import Path


def case_23():
    # --- #31: run_llm + run_claude alias, at a mocked subprocess boundary ---
    from aro import llm as _llm
    calls = []
    state = {"behavior": "ok"}

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if state["behavior"] == "timeout":
            raise _llm.subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if state["behavior"] == "spawn":
            raise FileNotFoundError("no binary")
        if state["behavior"] == "nonzero":
            return _types.SimpleNamespace(stdout="", stderr="kaput", returncode=7)
        if state["behavior"] == "grok-degraded":
            return _types.SimpleNamespace(
                stdout='{"text":"unsafe","usage":{"output_tokens":1}}',
                stderr="warning: sandbox could not be applied, continuing without sandbox",
                returncode=0)
        if state["behavior"] == "grok-workspace-degraded":
            return _types.SimpleNamespace(
                stdout='{"text":"unsafe","usage":{"output_tokens":1}}',
                stderr="warning: sandbox initialization failed; defaulting to no sandbox",
                returncode=0)
        if "--output-format" not in cmd:
            return _types.SimpleNamespace(stdout="plain-reply", stderr="", returncode=0)
        return _types.SimpleNamespace(
            stdout=('{"result":"ok-reply","usage":{"output_tokens":42},'
                    '"total_cost_usd":0.5}'), stderr="", returncode=0)

    old_run = _llm.subprocess.run
    old_bin = _llm.CLAUDE_BIN
    old_grok_bin = _llm.GROK_BIN
    old_fallback = _llm.CLAUDE_FALLBACK_MODELS
    old_selected = _llm.os.environ.get("ARO_LLM_BACKEND")
    _llm.subprocess.run = fake_run
    _llm.CLAUDE_BIN = "claude-test"
    _llm.GROK_BIN = "grok-test"
    _llm.CLAUDE_FALLBACK_MODELS = "sonnet"
    try:
        text, toks, cost = _llm.run_llm("hi", backend="claude", timeout=10)
        assert (text, toks, cost) == ("ok-reply", 42, 0.5)
        argv, kwargs = calls[-1]
        assert argv[0] == "claude-test" and "--fallback-model" in argv, argv
        assert kwargs["timeout"] == 10 and kwargs["capture_output"] and kwargs["text"]

        # Compatibility alias is always Claude, independent of global selection.
        _llm.os.environ["ARO_LLM_BACKEND"] = "codex"
        assert _llm.run_claude("hi", timeout=10) == ("ok-reply", 42, 0.5)
        assert calls[-1][0][0] == "claude-test", calls[-1][0]

        _llm.CLAUDE_FALLBACK_MODELS = ""
        _llm.run_claude("hi", timeout=10)
        assert "--fallback-model" not in calls[-1][0]
        raw, t0, c0 = _llm.run_claude("hi", timeout=10, json_output=False)
        assert (raw, t0, c0) == ("plain-reply", 0, 0.0)
        assert "--output-format" not in calls[-1][0]

        state["behavior"] = "grok-degraded"
        try:
            _llm.run_llm("hi", backend="grok", timeout=10, allow_write=False)
            raise AssertionError("grok read-only sandbox degradation must raise")
        except _llm.LLMError as e:
            assert "read-only sandbox" in str(e), e

        state["behavior"] = "grok-workspace-degraded"
        try:
            _llm.run_llm("hi", backend="grok", timeout=10, allow_write=True)
            raise AssertionError("grok workspace sandbox degradation must raise")
        except _llm.LLMError as e:
            assert "workspace sandbox" in str(e), e

        for behavior, needle in (("nonzero", "kaput"), ("timeout", "timed out"),
                                 ("spawn", "failed to launch")):
            state["behavior"] = behavior
            try:
                _llm.run_llm("hi", backend="claude", timeout=10)
                raise AssertionError(f"{behavior} must raise LLMError")
            except _llm.LLMError as e:
                assert needle in str(e), e
    finally:
        _llm.subprocess.run = old_run
        _llm.CLAUDE_BIN = old_bin
        _llm.GROK_BIN = old_grok_bin
        _llm.CLAUDE_FALLBACK_MODELS = old_fallback
        _llm.os.environ.pop("ARO_LLM_BACKEND", None)
        if old_selected is not None:
            _llm.os.environ["ARO_LLM_BACKEND"] = old_selected
    print("#31 OK: run_llm + pinned run_claude alias; hermetic success/raw/error policy")

def case_34():
    # --- #45: multi-backend LLM command construction (hermetic; no CLI spawn) ---
    from aro import llm as _llm

    old_bins = (_llm.CLAUDE_BIN, _llm.CODEX_BIN, _llm.GROK_BIN)
    old_fallback = _llm.CLAUDE_FALLBACK_MODELS
    old_codex_sandbox = _llm.ARO_CODEX_SANDBOX
    _llm.CLAUDE_BIN = "claude-test"
    _llm.CODEX_BIN = "codex-test"
    _llm.GROK_BIN = "grok-test"
    _llm.CLAUDE_FALLBACK_MODELS = "sonnet"
    try:
        claude = _llm.get_backend("claude")
        assert claude.build_cmd("hello", "/tmp/work", False, 600) == [
            "claude-test", "--fallback-model", "sonnet",
            "--output-format", "json", "-p", "hello"]
        assert claude.build_cmd("hello", "/tmp/work", True, 600) == [
            "claude-test", "--dangerously-skip-permissions",
            "--fallback-model", "sonnet", "--output-format", "json", "-p", "hello"]

        codex = _llm.get_backend("codex")
        assert codex.build_cmd("hello", "/tmp/work", False, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox", "read-only",
            "--json", "hello"]
        assert codex.build_cmd("hello", "/tmp/work", True, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox", "workspace-write",
            "--json", "hello"]

        # ARO_CODEX_SANDBOX escape hatch: write tier follows the override
        # (normalized), read tier stays pinned to read-only.
        _llm.ARO_CODEX_SANDBOX = " Danger-Full-Access "
        assert codex.build_cmd("hello", "/tmp/work", True, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox",
            "danger-full-access", "--json", "hello"]
        assert codex.build_cmd("hello", "/tmp/work", False, 600) == [
            "codex-test", "exec", "-C", "/tmp/work", "--sandbox", "read-only",
            "--json", "hello"]
        assert codex.write_sandbox == "danger-full-access"
        _llm.ARO_CODEX_SANDBOX = "yolo"
        try:
            codex.build_cmd("hello", "/tmp/work", True, 600)
            raise AssertionError("invalid ARO_CODEX_SANDBOX must raise")
        except _llm.LLMError as e:
            msg = str(e)
            assert all(s in msg for s in
                       ("yolo", "workspace-write", "danger-full-access")), msg
        # an invalid value must not break read-only calls (critic path)
        assert codex.build_cmd("hello", "/tmp/work", False, 600)[5] == "read-only"
        _llm.ARO_CODEX_SANDBOX = ""
        assert codex.write_sandbox == "workspace-write"

        grok = _llm.get_backend("grok")
        assert grok.build_cmd("hello", "/tmp/work", False, 600) == [
            "grok-test", "-p", "hello", "--cwd", "/tmp/work",
            "--output-format", "json", "--max-turns", "100",
            "--sandbox", "aro-read-only"]
        assert grok.build_cmd("hello", "/tmp/work", True, 600) == [
            "grok-test", "-p", "hello", "--cwd", "/tmp/work",
            "--output-format", "json", "--max-turns", "100",
            "--sandbox", "aro-workspace", "--always-approve"]

        assert claude.parse_reply(
            '{"result":"claude-answer","usage":{"output_tokens":5},'
            '"total_cost_usd":0.25}', "", 0) == ("claude-answer", 5, 0.25)
        assert _llm.parse_json_reply("legacy plain reply") == (
            "legacy plain reply", 0, 0.0)
        codex_jsonl = "\n".join([
            '{"type":"thread.started","thread_id":"t"}',
            '{"type":"item.completed","item":{"id":"i0",'
            '"type":"agent_message","text":"working"}}',
            '{"type":"item.completed","item":{"id":"i1",'
            '"type":"agent_message","text":"codex-answer"}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,'
            '"cached_input_tokens":2,"output_tokens":3}}',
        ])
        assert codex.parse_reply(codex_jsonl, "", 0) == ("codex-answer", 3, None)
        assert grok.parse_reply(
            '{"text":"grok-answer","usage":{"output_tokens":7},'
            '"total_cost_usd":0.125}', "", 0) == ("grok-answer", 7, 0.125)

        for backend in (claude, codex, grok):
            try:
                backend.parse_reply("not-json", "", 0)
                raise AssertionError(f"{backend.name} malformed reply must raise")
            except _llm.LLMError:
                pass
            try:
                backend.parse_reply("{}", "backend failed", 7)
                raise AssertionError(f"{backend.name} nonzero reply must raise")
            except _llm.LLMError as e:
                assert "7" in str(e) and "backend failed" in str(e), e
        for backend, malformed in (
                (claude, '{"result":"ok","usage":[]}'),
                (codex, '{"type":"item.completed","item":[]}'),
                (grok, '{"text":"ok","usage":[]}')):
            try:
                backend.parse_reply(malformed, "", 0)
                raise AssertionError(f"{backend.name} malformed shape must raise")
            except _llm.LLMError:
                pass
        try:
            codex.parse_reply('{"type":"turn.completed","usage":{}}', "", 0)
            raise AssertionError("codex reply without an agent message must raise")
        except _llm.LLMError:
            pass

        old_selected = _llm.os.environ.pop("ARO_LLM_BACKEND", None)
        try:
            spec_cfg = _types.SimpleNamespace(llm_backend="codex", critic_backend=None)
            assert _llm.select_backend().name == "claude"
            assert _llm.select_backend(spec_cfg).name == "codex"
            _llm.os.environ["ARO_LLM_BACKEND"] = "grok"
            assert _llm.select_backend(spec_cfg).name == "grok"
            cross_model = _types.SimpleNamespace(
                llm_backend="claude", critic_backend="codex")
            assert _llm.select_backend(cross_model, critic=True).name == "codex"
            del _llm.os.environ["ARO_LLM_BACKEND"]
            try:
                _llm.select_backend(
                    _types.SimpleNamespace(llm_backend="mystery", critic_backend=None))
                raise AssertionError("unknown backend must raise")
            except _llm.LLMError as e:
                msg = str(e)
                assert all(s in msg for s in ("mystery", "claude", "codex", "grok")), msg
        finally:
            _llm.os.environ.pop("ARO_LLM_BACKEND", None)
            if old_selected is not None:
                _llm.os.environ["ARO_LLM_BACKEND"] = old_selected

        runtime_selected = _llm.os.environ.pop("ARO_LLM_BACKEND", None)
        try:
            from aro import critic as _critic, generator as _generator
            target = _types.SimpleNamespace(
                spec=_types.SimpleNamespace(llm_backend="codex", critic_backend=None),
                repo=Path("/tmp/work"))
            assert _generator.RalphGenerator(target).backend.name == "codex"
            assert _generator.AgenticGenerator(target).backend.name == "codex"
            with tempfile.TemporaryDirectory() as agent_tmp:
                scratch = Path(agent_tmp).resolve()
                cargo_td = Path(_generator._agent_env(scratch)["CARGO_TARGET_DIR"])
                assert cargo_td.parent == scratch and cargo_td.name == ".aro-agent-target"

            critic_calls = []
            old_run_llm = _llm.run_llm

            def fake_llm(prompt, **kwargs):
                critic_calls.append(kwargs["backend"].name)
                return '{"verdict":"pass","reasons":[]}', 9, None

            _llm.run_llm = fake_llm
            try:
                critique = _critic.critique(
                    "code", "candidate", backend=_llm.get_backend("grok"))
                assert critique.verdict == "pass" and critique.tokens == 9
                assert critic_calls == ["grok"], critic_calls
            finally:
                _llm.run_llm = old_run_llm
        finally:
            if runtime_selected is not None:
                _llm.os.environ["ARO_LLM_BACKEND"] = runtime_selected
    finally:
        _llm.CLAUDE_BIN, _llm.CODEX_BIN, _llm.GROK_BIN = old_bins
        _llm.CLAUDE_FALLBACK_MODELS = old_fallback
        _llm.ARO_CODEX_SANDBOX = old_codex_sandbox

    import importlib as _importlib
    override_names = ("ARO_CLAUDE_BIN", "ARO_CODEX_BIN", "ARO_GROK_BIN")
    old_overrides = {name: _llm.os.environ.get(name) for name in override_names}
    try:
        for name, value in zip(override_names, ("c-env", "x-env", "g-env")):
            _llm.os.environ[name] = value
        _importlib.reload(_llm)
        assert _llm.get_backend("claude").build_cmd("p", "/w", False, 1)[0] == "c-env"
        assert _llm.get_backend("codex").build_cmd("p", "/w", False, 1)[0] == "x-env"
        assert _llm.get_backend("grok").build_cmd("p", "/w", False, 1)[0] == "g-env"
    finally:
        for name, value in old_overrides.items():
            if value is None:
                _llm.os.environ.pop(name, None)
            else:
                _llm.os.environ[name] = value
        _importlib.reload(_llm)
    print("#45a-e OK: commands/parsing/config/topology + binary env overrides")

