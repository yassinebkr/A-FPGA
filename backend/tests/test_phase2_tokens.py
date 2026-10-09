"""Phase 2 token accounting and context admission contracts."""

import asyncio
from types import SimpleNamespace

import agent_core


def test_prompt_estimate_is_deterministic_and_includes_tools():
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "probe"}}]
    first = agent_core.estimate_prompt_tokens(messages, tools)
    second = agent_core.estimate_prompt_tokens(messages, tools)
    assert first == second
    assert first > agent_core.estimate_prompt_tokens(messages, [])
    assert agent_core.estimate_tokens("abcd", bytes_per_token=4) == 1
    assert agent_core.estimate_tokens("abcde", bytes_per_token=4) == 2


def test_session_api_usage_does_not_include_next_prompt_estimate():
    sid = "phase2-token-separation"
    agent_core.reset_session(sid)
    agent_core._record_usage(sid, SimpleNamespace(
        prompt_tokens=11, completion_tokens=7, total_tokens=18))
    preflight = agent_core._context_preflight(
        sid, [{"role": "user", "content": "x"}], [])
    usage = agent_core.session_api_usage(sid)
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens"] == 7
    assert usage["total_tokens"] == 18
    assert preflight["estimate"] > 0
    assert agent_core._get_state(sid)["next_prompt_estimate"] == preflight["estimate"]
    assert agent_core._get_state(sid)["tokens_used"] == 18
    # The counters are separate even when numeric values happen to coincide.
    agent_core.reset_session(sid)


def _fake_response(text="done", usage=None):
    message = SimpleNamespace(
        content=text, tool_calls=None,
        model_dump=lambda exclude_none=True: {"role": "assistant", "content": text})
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_soft_context_limit_warns_once_before_api_call(monkeypatch):
    sid = "phase2-soft-context"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "CONTEXT_SOFT_LIMIT", 1)
    monkeypatch.setattr(agent_core, "CONTEXT_HARD_LIMIT", 10_000_000)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return _fake_response(usage=SimpleNamespace(
            prompt_tokens=20, completion_tokens=3, total_tokens=23))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))
    events = asyncio.run(
        _collect(agent_core.chat("hello", sid)))
    assert len(calls) == 1
    warnings = [e for e in events if e["type"] == "status"
                and "Context warning" in e["content"]]
    assert len(warnings) == 1
    assert agent_core.session_api_usage(sid)["total_tokens"] == 23
    assert agent_core._get_state(sid)["next_prompt_estimate"] > 0
    agent_core.reset_session(sid)


def test_hard_context_limit_blocks_api_call(monkeypatch):
    sid = "phase2-hard-context"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "CONTEXT_SOFT_LIMIT", 1)
    monkeypatch.setattr(agent_core, "CONTEXT_HARD_LIMIT", 1)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return _fake_response()

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))
    events = asyncio.run(
        _collect(agent_core.chat("hello", sid)))
    assert calls == []
    assert any(e["type"] == "text" and "hard context limit" in e["content"]
               for e in events)
    assert agent_core.session_api_usage(sid)["total_tokens"] == 0
    agent_core.reset_session(sid)


async def _collect(generator):
    return [event async for event in generator]
