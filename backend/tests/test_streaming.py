"""Phase 1 provider-stream transport tests."""
import asyncio
from types import SimpleNamespace

import agent_core


def _chunk(*, content=None, reasoning=None, tool_calls=None, usage=None,
           finish_reason=None, choices=True):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        reasoning=None,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)] if choices else [],
        usage=usage,
    )


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        item = self._chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _lock_test_contract(sid):
    payload = {
        "target_type": "asic", "top_module": "dut", "technology": "sky130",
        "clock_target_mhz": 50, "clock_period_ns": 20, "latency_cycles": 2,
        "throughput_per_cycle": 1, "pipeline_stages": 1,
        "multiplier_strategy": "inferred", "memory_strategy": "registers",
    }
    agent_core._begin_chat_run(sid, require_architecture=True)
    raw = agent_core.define_architecture(**payload)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload, raw)
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"])


def _call(index, *, ident=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=ident,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_streamed_text_reasoning_usage_and_history_are_canonical(monkeypatch):
    sid = "stream-canonical"
    agent_core.reset_session(sid)
    calls = []
    stream = _Stream([
        _chunk(reasoning="inspect the interface"),
        _chunk(content="The design is ready."),
        _chunk(usage=SimpleNamespace(prompt_tokens=4, completion_tokens=7,
                                     total_tokens=11), choices=False),
    ])

    async def create(**kwargs):
        calls.append(kwargs)
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("inspect", sid)]

    try:
        events = asyncio.run(run())
        assert calls[0]["stream"] is True
        assert calls[0]["stream_options"] == {"include_usage": True}
        assert [e["content"] for e in events if e["type"] == "reasoning_delta"] == [
            "inspect the interface"]
        assert [e["content"] for e in events if e["type"] == "text_delta"] == [
            "The design is ready."]
        assert not [e for e in events if e["type"] == "text"]
        starts = [e for e in events if e["type"] == "generation_start"]
        assert len(starts) == 1 and starts[0]["generation_id"].startswith("gen-")
        usage = [e for e in events if e["type"] == "generation_usage"]
        assert usage and usage[0]["input_tokens"] == 4
        assert usage[0]["output_tokens"] == 7
        assert usage[0]["total_tokens"] == 11
        assert any(e["type"] == "generation_end" for e in events)
        assert agent_core.session_api_usage(sid)["total_tokens"] == 11
        history = agent_core._get_history(sid)
        assistants = [e for e in history if e.get("role") == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["content"] == "The design is ready."
        assert "reasoning" not in assistants[0]
    finally:
        agent_core.reset_session(sid)


def test_mistral_typed_content_chunks_stream_reasoning_and_text(monkeypatch):
    """Mistral reasoning models (zai-glm-5-3) stream typed chunks inside
    delta.content instead of a plain string: ThinkChunk(type="thinking",
    inner text chunks) then TextChunk(type="text"). The consumer must unpack
    the thinking into reasoning_delta events, keep the answer as text, and
    feed the first-token watchdog so long thinking never trips the timeout."""
    sid = "stream-mistral-chunks"
    agent_core.reset_session(sid)
    stream = _Stream([
        _chunk(content=[{"type": "thinking",
                         "thinking": [{"type": "text", "text": "consider the MAC array"}],
                         "closed": True}]),
        _chunk(content=[{"type": "thinking",
                         "thinking": [{"type": "text", "text": " then the pipeline"}]}]),
        _chunk(content=[{"type": "text", "text": "The array is designed."}]),
    ])

    async def create(**kwargs):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        assert "".join(e["content"] for e in events
                       if e["type"] == "reasoning_delta") == \
            "consider the MAC array then the pipeline"
        assert [e["content"] for e in events if e["type"] == "text_delta"] == [
            "The array is designed."]
        assert not [e for e in events if e["type"] == "error"]
        history = agent_core._get_history(sid)
        assistants = [e for e in history if e.get("role") == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["content"] == "The array is designed."
    finally:
        agent_core.reset_session(sid)


def test_typed_content_chunk_parsing_edge_cases():
    parse = agent_core._typed_content_chunk_items
    # Plain-string content is not a chunk list.
    assert parse("plain") == []
    assert parse(None) == []
    # String-form thinking, unknown types, and non-dict items are handled.
    assert parse([{"type": "thinking", "thinking": "free-form"}]) == [
        ("thinking", "free-form")]
    assert parse([{"type": "image", "url": "x"}, "junk", {}]) == []
    assert parse([{"type": "text", "text": ""}]) == []


def test_long_provider_reasoning_is_forwarded_without_truncation(monkeypatch):
    sid = "stream-long-reasoning"
    agent_core.reset_session(sid)
    chunks = ["r%05d\n" % i for i in range(5000)]
    stream = _Stream([
        _chunk(reasoning=part) for part in chunks
    ] + [_chunk(content="done")])

    async def create(**kwargs):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        reasoning = "".join(
            event["content"] for event in events
            if event["type"] == "reasoning_delta"
        )
        assert reasoning == "".join(chunks)
        assert "reasoning output truncated" not in reasoning
    finally:
        agent_core.reset_session(sid)


def test_fragmented_tool_call_is_assembled_once(monkeypatch):
    sid = "stream-tool-fragments"
    agent_core.reset_session(sid)
    stream = _Stream([
        _chunk(tool_calls=[_call(0, ident="call-1", name="run_simulation",
                                 arguments='{"verilog_code":"v",')]),
        _chunk(tool_calls=[_call(0, arguments='"testbench_code":"tb",')]),
        _chunk(tool_calls=[_call(0, arguments='"top_module":"m"}')]),
    ])
    calls = []

    async def create(**kwargs):
        return stream

    async def fake_tool(**kwargs):
        calls.append(kwargs)
        return "SIMULATION PASSED"

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "run_simulation", fake_tool)

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        assert len(calls) == 1
        assert calls[0] == {"verilog_code": "v", "testbench_code": "tb",
                            "top_module": "m"}
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == 1
    finally:
        agent_core.reset_session(sid)


def test_thinking_chunk_and_tool_call_share_one_delta(monkeypatch):
    """GLM edge case: a streamed delta can carry a typed thinking chunk and
    a tool-call fragment at the same time. Both must be handled."""
    sid = "stream-mixed-delta"
    agent_core.reset_session(sid)
    stream = _Stream([
        _chunk(content=[{"type": "thinking",
                         "thinking": [{"type": "text", "text": "plan the call"}]}],
               tool_calls=[_call(0, ident="call-7", name="run_simulation",
                                 arguments='{"verilog_code":"v",')]),
        _chunk(tool_calls=[_call(0, arguments='"testbench_code":"tb",')]),
        _chunk(tool_calls=[_call(0, arguments='"top_module":"m"}')]),
    ])
    calls = []

    async def create(**kwargs):
        return stream

    async def fake_tool(**kwargs):
        calls.append(kwargs)
        return "SIMULATION PASSED"

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "run_simulation", fake_tool)

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        assert [e["content"] for e in events
                if e["type"] == "reasoning_delta"] == ["plan the call"]
        assert len(calls) == 1
        assert calls[0] == {"verilog_code": "v", "testbench_code": "tb",
                            "top_module": "m"}
    finally:
        agent_core.reset_session(sid)


def test_model_effort_kwargs_follow_the_registry_default(monkeypatch):
    """Every GLM request carries the registry's default reasoning effort
    (the platform's serving default is high, which burned ~30k thinking
    tokens on a small-design round); the env override is honored and any
    value outside the declared list is dropped rather than sent."""
    monkeypatch.delenv("GLM_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(agent_core, "_session_model_key",
                        lambda _: "mistral-glm-5.3")
    assert agent_core._model_effort_kwargs("s") == {
        "extra_body": {"reasoning_effort": "low"}}
    monkeypatch.setenv("GLM_REASONING_EFFORT", "high")
    assert agent_core._model_effort_kwargs("s") == {
        "extra_body": {"reasoning_effort": "high"}}
    monkeypatch.setenv("GLM_REASONING_EFFORT", "none")  # rejected by the API
    assert agent_core._model_effort_kwargs("s") == {}
    monkeypatch.setattr(agent_core, "_session_model_key", lambda _: "kimi-k3")
    assert agent_core._model_effort_kwargs("s") == {}


def test_continuation_kwargs_follow_model_capabilities(monkeypatch):
    """Only models that declare effort levels get the low-effort retry knob."""
    monkeypatch.setattr(agent_core, "_session_model_key",
                        lambda _: "mistral-glm-5.3")
    assert agent_core._continuation_request_kwargs("s") == {
        "extra_body": {"reasoning_effort": "low"}}
    monkeypatch.setattr(agent_core, "_session_model_key", lambda _: "kimi-k3")
    assert agent_core._continuation_request_kwargs("s") == {}
    monkeypatch.setattr(agent_core, "_session_model_key", lambda _: "unknown-key")
    assert agent_core._continuation_request_kwargs("s") == {}


def test_reasoning_length_stop_gets_low_effort_continuation(monkeypatch):
    """GLM failure mode: thinking exhausts max_tokens, the provider stops with
    finish_reason=length and no visible output. The bounded continuation must
    re-issue the request with low reasoning effort so the retry can fit."""
    sid = "stream-length-continuation"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "_session_model_key",
                        lambda _: "mistral-glm-5.3")
    # The per-machine effort is raised so the default-effort request differs
    # from the continuation's forced low-effort retry.
    monkeypatch.setenv("GLM_REASONING_EFFORT", "max")
    streams = [_Stream([_chunk(finish_reason="length")]),
               _Stream([_chunk(content="Recovered answer.")])]
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return streams.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        assert len(calls) == 2
        assert calls[0]["extra_body"] == {"reasoning_effort": "max"}
        assert calls[1]["extra_body"] == {"reasoning_effort": "low"}
        assert any("output limit reached" in e.get("content", "")
                   for e in events if e["type"] == "status")
        assert [e["content"] for e in events if e["type"] == "text_delta"] == [
            "Recovered answer."]
        st = agent_core._get_state(sid)
        assert st["continuation_instruction"] is None
        assert st["continuation_kwargs"] is None
        assert st["empty_continuations"] == 1
    finally:
        agent_core.reset_session(sid)


def test_repeated_reasoning_length_stop_stops_honestly(monkeypatch):
    """A second consecutive length stop with no output exhausts the single
    bounded continuation: the run stops with the honest empty-response error
    and never enters an unbounded paid retry loop (Kimi has no effort knob,
    so its retry request is identical)."""
    sid = "stream-length-exhausted"
    agent_core.reset_session(sid)
    streams = [_Stream([_chunk(finish_reason="length")]),
               _Stream([_chunk(finish_reason="length")])]
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return streams.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        assert len(calls) == 2
        assert all("extra_body" not in c for c in calls)
        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1
        assert "empty response" in errors[0]["content"]
        assert "finish_reason=length" in errors[0]["content"]
        assert agent_core._get_state(sid)["empty_continuations"] == 1
    finally:
        agent_core.reset_session(sid)


def test_first_token_timeout_cancels_stream(monkeypatch):
    sid = "stream-first-timeout"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "STREAM_FIRST_TOKEN_TIMEOUT_S", 0.03)
    monkeypatch.setattr(agent_core, "STREAM_INACTIVITY_TIMEOUT_S", 0.03)
    gate = asyncio.Event()

    class SlowStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await gate.wait()
            raise StopAsyncIteration

    async def create(**kwargs):
        return SlowStream()

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))
    monkeypatch.setattr(agent_core, "STATUS_HEARTBEAT_S", 0.01)

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        assert any(e["type"] == "error" and "first-token timeout" in e["content"]
                   for e in events)
        assert not [e for e in events if e["type"] == "text_delta"]
    finally:
        gate.set()
        agent_core.reset_session(sid)



def test_active_stream_is_not_cut_off_by_wall_clock(monkeypatch):
    """An active provider stream relies on inactivity, not a global 900s cap."""
    sid = "stream-active-no-overall-cutoff"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "STREAM_OVERALL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(agent_core, "STREAM_FIRST_TOKEN_TIMEOUT_S", 0.1)
    monkeypatch.setattr(agent_core, "STREAM_INACTIVITY_TIMEOUT_S", 0.1)

    class SlowStream:
        def __init__(self):
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index >= 3:
                raise StopAsyncIteration
            await asyncio.sleep(0.02)
            self.index += 1
            if self.index == 1:
                return _chunk(content="a")
            if self.index == 2:
                return _chunk(content="b")
            return _chunk(usage=SimpleNamespace(prompt_tokens=1,
                                                 completion_tokens=2,
                                                 total_tokens=3),
                          choices=False)

    async def create(**kwargs):
        return SlowStream()

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        assert not [e for e in events if e["type"] == "error"]
        assert [e["content"] for e in events if e["type"] == "text_delta"] == ["a", "b"]
        assert agent_core._get_history(sid)[-1]["content"] == "ab"
    finally:
        agent_core.reset_session(sid)


def test_full_reasoning_is_forwarded_without_truncation(monkeypatch):
    sid = "stream-full-reasoning"
    agent_core.reset_session(sid)
    long_reasoning = "r" * 25_000
    stream = _Stream([_chunk(reasoning=long_reasoning, content="done")])

    async def create(**kwargs):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        reasoning = "".join(e["content"] for e in events
                             if e["type"] == "reasoning_delta")
        assert reasoning == long_reasoning
        assert "[reasoning output truncated]" not in reasoning
    finally:
        agent_core.reset_session(sid)


def test_inactivity_timeout_preserves_safe_partial_text(monkeypatch):
    sid = "stream-partial-timeout"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "STREAM_FIRST_TOKEN_TIMEOUT_S", 0.1)
    monkeypatch.setattr(agent_core, "STREAM_INACTIVITY_TIMEOUT_S", 0.03)
    gate = asyncio.Event()

    class PartialThenSlowStream:
        def __init__(self):
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index == 0:
                self.index += 1
                return _chunk(content="partial answer")
            await gate.wait()
            raise StopAsyncIteration

    async def create(**kwargs):
        return PartialThenSlowStream()

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))
    monkeypatch.setattr(agent_core, "STATUS_HEARTBEAT_S", 0.01)

    async def run():
        return [event async for event in agent_core.chat("run", sid)]

    try:
        events = asyncio.run(run())
        errors = [e for e in events if e["type"] == "error"]
        assert errors and "Partial assistant text was preserved" in errors[-1]["content"]
        ends = [e for e in events if e["type"] == "generation_end"]
        assert ends and ends[-1]["partial_saved"] is True
        assistants = [e for e in agent_core._get_history(sid)
                      if e.get("role") == "assistant"]
        assert assistants and assistants[-1]["content"].startswith("partial answer")
        assert "interrupted" in assistants[-1]["content"]
    finally:
        gate.set()
        agent_core.reset_session(sid)



def test_length_finished_reason_gets_one_bounded_continuation(monkeypatch):
    sid = "stream-length-continuation"
    agent_core.reset_session(sid)
    streams = [
        _Stream([
            _chunk(reasoning="private planning"),
            _chunk(finish_reason="length"),
            _chunk(usage=SimpleNamespace(prompt_tokens=10,
                                         completion_tokens=20,
                                         total_tokens=30), choices=False),
        ]),
        _Stream([
            _chunk(content="continued answer", finish_reason="stop"),
            _chunk(usage=SimpleNamespace(prompt_tokens=12,
                                         completion_tokens=4,
                                         total_tokens=16), choices=False),
        ]),
    ]
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return streams.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("continue", sid)]

    try:
        events = asyncio.run(run())
        assert len(calls) == 2
        assert [e["content"] for e in events if e["type"] == "text_delta"] == [
            "continued answer"]
        ends = [e for e in events if e["type"] == "generation_end"]
        assert [e.get("finish_reason") for e in ends] == ["length", "stop"]
        assert any(e["type"] == "status" and "continuing once" in e["content"]
                   for e in events)
        assert not [e for e in events if e["type"] == "error"]
        assert agent_core._get_state(sid)["continuation_instruction"] is None
        assert agent_core.session_api_usage(sid)["total_tokens"] == 46
    finally:
        agent_core.reset_session(sid)


def test_empty_response_reports_finish_reason_without_retry(monkeypatch):
    sid = "stream-empty-finish-reason"
    agent_core.reset_session(sid)
    monkeypatch.setattr(agent_core, "MAX_EMPTY_CONTINUATIONS", 0)
    stream = _Stream([
        _chunk(finish_reason="content_filter"),
        _chunk(usage=SimpleNamespace(prompt_tokens=2,
                                     completion_tokens=3,
                                     total_tokens=5), choices=False),
    ])

    async def create(**kwargs):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("empty", sid)]

    try:
        events = asyncio.run(run())
        errors = [e for e in events if e["type"] == "error"]
        assert errors
        assert "Provider finish_reason=content_filter" in errors[-1]["content"]
        assert any(e.get("finish_reason") == "content_filter"
                   for e in events if e["type"] == "generation_end")
    finally:
        agent_core.reset_session(sid)


def test_implicit_retry_persists_and_stale_approval_gets_bounded_continuation(monkeypatch):
    sid = "stream-stale-approval"
    agent_core.reset_session(sid)
    agent_core._get_history(sid).append(
        {"role": "user", "content": "Implement the dut accelerator"})
    _lock_test_contract(sid)
    streams = [
        _Stream([_chunk(content="Use the Approve & Build button to continue.",
                        finish_reason="stop")]),
        _Stream([_chunk(content="Continuing the locked implementation.",
                        finish_reason="stop")]),
    ]
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return streams.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))

    async def run():
        return [event async for event in agent_core.chat("retry", sid)]

    try:
        events = asyncio.run(run())
        assert len(calls) == 2
        history = agent_core._get_history(sid)
        assert any(entry.get("role") == "user" and entry.get("content") == "retry"
                   for entry in history)
        assert not any(entry.get("role") == "assistant" and
                       "Approve & Build" in str(entry.get("content") or "")
                       for entry in history)
        assert any(event.get("type") == "status" and "No approval is pending"
                   in event.get("content", "") for event in events)
        assert any(event.get("type") == "text_delta" and
                   "Continuing the locked implementation" in event.get("content", "")
                   for event in events)
        assert agent_core._get_state(sid)["stale_approval_continuations"] == 1
    finally:
        agent_core.reset_session(sid)


def test_effort_scales_run_budget_and_request_kwargs(monkeypatch):
    """The run budget scales with the session's effective effort (project
    setting first, then the registry default), and the request carries the
    same effort. Without an effort the base budget applies unchanged."""
    monkeypatch.delenv("GLM_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(agent_core, "_session_model_key",
                        lambda _: "mistral-glm-5.3")
    monkeypatch.setattr(agent_core, "CHAT_TOKEN_BUDGET", 1000)
    assert agent_core._session_reasoning_effort("s") == "low"
    assert agent_core._chat_budget("s") == 1000
    agent_core._project_efforts["s"] = "high"
    try:
        assert agent_core._session_reasoning_effort("s") == "high"
        assert agent_core._model_effort_kwargs("s") == {
            "extra_body": {"reasoning_effort": "high"}}
        assert agent_core._chat_budget("s") == 2000
        agent_core._project_efforts["s"] = "max"
        assert agent_core._chat_budget("s") == 4000
        # The pinned per-run budget drives preflight admission.
        sid = "s-budget-preflight"
        agent_core.reset_session(sid)
        st = agent_core._get_state(sid)
        st["chat_budget"] = 2000
        st["tokens_used"] = 1500
        st["chat_token_start"] = 0
        monkeypatch.setattr(agent_core, "CONTEXT_HARD_LIMIT", 100_000)
        result = agent_core._context_preflight(
            sid, [{"role": "user", "content": "small"}], [])
        assert result["run_remaining"] == 500
    finally:
        agent_core._project_efforts.pop("s", None)
