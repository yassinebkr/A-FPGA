"""Tests for the LLM-wait heartbeat (agent_core STATUS_HEARTBEAT_S): while a
chat completion is in flight, chat() must yield periodic status events so the
UI can show liveness during multi-minute first-round generations."""
import asyncio
import types

import agent_core


def _fake_response(text):
    msg = types.SimpleNamespace(
        content=text,
        tool_calls=None,
        model_dump=lambda exclude_none=True: {"role": "assistant", "content": text},
    )
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=None)


def _fake_client(gate, response):
    async def create(**kwargs):
        await gate.wait()
        return response

    chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    return types.SimpleNamespace(chat=chat)


def test_status_heartbeat_emitted_while_waiting(monkeypatch):
    """A completion that blocks must produce an immediate status event plus
    periodic elapsed-time updates before the response arrives."""
    monkeypatch.setattr(agent_core, "STATUS_HEARTBEAT_S", 0.05)

    async def main():
        gate = asyncio.Event()
        client = _fake_client(gate, _fake_response("all done"))
        monkeypatch.setattr(agent_core, "_client_for", lambda sid: (client, "fake-model"))

        gen = agent_core.chat("build me a counter", session_id="hbtest")
        events = []
        # Keep consuming while the completion is blocked until a periodic
        # heartbeat (the one carrying elapsed time) has arrived.
        async for event in gen:
            events.append(event)
            if (event.get("type") == "status"
                    and "elapsed (round 1)" in event.get("content", "")):
                break
        gate.set()
        rest = [event async for event in gen]
        return events + rest

    try:
        events = asyncio.run(main())
    finally:
        agent_core.reset_session("hbtest")

    # Round-boundary run_state snapshots and streaming generation frames are
    # asserted by their own suites; this test targets heartbeat liveness only.
    statuses = [event for event in events if event.get("type") == "status"]
    texts = [event for event in events if event.get("type") == "text"]
    assert statuses, "no status events were emitted while waiting"
    assert statuses[0]["content"] == "Waiting for model response… (round 1)"
    assert any("elapsed (round 1)" in status["content"] for status in statuses[1:]), \
        "no periodic heartbeat was emitted while the completion was blocked"
    assert texts[-1] == {"type": "text", "content": "all done"}
