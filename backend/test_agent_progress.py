"""Focused tests for public structured run progress/decision frames."""
import json
from types import SimpleNamespace
from uuid import uuid4

import asyncio

import agent_core


class _FakeMessage:
    def __init__(self, *, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True):
        result = {"role": "assistant"}
        if self.content is not None:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = [{
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            } for call in self.tool_calls]
        return result


class _FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=next(self.responses))], usage=None)


def test_model_preflight_emits_public_progress_without_reasoning(monkeypatch):
    session = f"progress-{uuid4().hex}"
    fake = _FakeClient([_FakeMessage(content="done")])
    monkeypatch.setattr(agent_core, "_client_for", lambda sid: (fake, "test-model"))
    async def collect():
        return [event async for event in agent_core._chat_inner("build a counter", session)]
    events = asyncio.run(collect())
    progress = [event for event in events if event["type"] == "progress"]
    assert progress
    payload = progress[0]["content"]
    assert payload["phase"] == "context"
    assert payload["status"] == "done"
    assert "estimated prompt" in payload["evidence"]
    assert not any(key in payload for key in ("analysis", "thought", "chain_of_thought"))


def test_tool_start_and_result_emit_replayable_progress(monkeypatch):
    session = f"progress-tool-{uuid4().hex}"
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="write_and_check_syntax",
            arguments=json.dumps({"verilog_code": "module dut; endmodule", "top_module": "dut"}),
        ),
    )
    fake = _FakeClient([
        _FakeMessage(tool_calls=[call]),
        _FakeMessage(content="verified"),
    ])
    monkeypatch.setattr(agent_core, "_client_for", lambda sid: (fake, "test-model"))
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "write_and_check_syntax", lambda **kwargs: json.dumps({"ok": True, "verdict": "ok"}))

    async def collect():
        return [event async for event in agent_core._chat_inner("lint this", session)]
    events = asyncio.run(collect())
    progress = [event for event in events if event["type"] == "progress"]
    actions = [event["content"]["action"] for event in progress]
    assert any(action in actions for action in ("Running write_and_check_syntax", "Linting"))
    assert any(action in actions for action in ("write_and_check_syntax finished", "Lint passed"))
    assert any(event["content"]["phase"] == "context" for event in progress)
    assert any(event["type"] == "tool_call" for event in events)
    assert any(event["type"] == "tool_result" for event in events)
    for event in progress:
        assert isinstance(event["content"], dict)
        assert "analysis" not in event["content"]


def test_revision_and_stop_decisions_are_concise_public_events():
    accepted = agent_core._decision_event_for_result(
        "run_silicon_compiler", "REVISION ACCEPTED: checkpoint r2; setup slack 0.4 ns")
    rejected = agent_core._decision_event_for_result(
        "run_silicon_compiler", "REVISION REJECTED: no improvement")
    stopped = agent_core._stop_decision_event("Run stopped: synthesis invocation limit reached")
    assert accepted["type"] == "decision"
    assert accepted["content"]["status"] == "accepted"
    assert rejected["content"]["status"] == "rejected"
    assert stopped["content"]["status"] == "blocked"
    for event in (accepted, rejected, stopped):
        assert "analysis" not in event["content"]
        assert "chain_of_thought" not in event["content"]


def test_public_timeline_is_durable_bounded_and_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    sid = "proj-timeline-persist"
    agent_core._histories[sid] = [{"role": "system", "content": "sys"}]
    agent_core._project_names[sid] = "Timeline"
    try:
        for index in range(agent_core.PUBLIC_TIMELINE_MAX + 7):
            assert agent_core.record_public_timeline_event(sid, {
                "type": "progress",
                "content": {
                    "event_id": f"event-{index}",
                    "phase": "synthesis",
                    "action": f"Attempt {index}",
                    "status": "done",
                    "analysis": "must never be persisted",
                },
            })
        data = agent_core._projects().load(sid)
        assert len(data["timeline"]) == agent_core.PUBLIC_TIMELINE_MAX
        assert data["timeline"][0]["content"]["event_id"] == "event-7"
        assert "analysis" not in data["timeline"][0]["content"]

        # A repeated event id updates in place instead of duplicating on replay.
        agent_core.record_public_timeline_event(sid, {
            "type": "decision",
            "content": {"event_id": "event-99", "phase": "run",
                        "action": "final decision", "status": "accepted"},
        })
        agent_core.record_public_timeline_event(sid, {
            "type": "decision",
            "content": {"event_id": "event-99", "phase": "run",
                        "action": "updated decision", "status": "rejected"},
        })
        agent_core.record_public_timeline_event(sid, {
            "type": "decision",
            "content": {
                "event_id": "telemetry", "phase": "revision",
                "action": "bounded telemetry", "status": "rejected",
                "attempts": {"round": 2, "tool": 3, "nested": {"value": 7}},
                "budgets": {"tokens": {"used": 41, "limit": 1000, "remaining": 959},
                            "tools": {"used": 2, "limit": 10}},
                "metric_delta": {"setupslack": -0.67, "cells": -4},
            },
        })
        state = agent_core.get_project_state(sid)
        matches = [item for item in state["timeline"]
                   if item["content"]["event_id"] == "event-99"]
        assert len(matches) == 1
        assert matches[0]["content"]["action"] == "updated decision"
        telemetry = next(item for item in state["timeline"]
                         if item["content"]["event_id"] == "telemetry")["content"]
        assert telemetry["attempts"]["round"] == 2
        assert telemetry["attempts"]["nested"]["value"] == 7
        assert telemetry["budgets"]["tokens"]["remaining"] == 959
        assert telemetry["metric_delta"]["setupslack"] == -0.67

        # Simulate a fresh process loading the persisted project.
        agent_core._histories.pop(sid, None)
        agent_core._project_timelines.pop(sid, None)
        reloaded = agent_core.get_project_state(sid)["timeline"]
        assert any(item["content"]["event_id"] == "event-99" and
                   item["content"]["action"] == "updated decision" for item in reloaded)
        assert any(item["content"]["event_id"] == "telemetry" and
                   item["content"]["budgets"]["tokens"]["used"] == 41 for item in reloaded)
    finally:
        agent_core._histories.pop(sid, None)
        agent_core._project_timelines.pop(sid, None)
        agent_core._project_names.pop(sid, None)
