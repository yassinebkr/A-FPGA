"""Public Phase 3 timeline contract tests (no model or EDA calls)."""

import asyncio
import json
import types

import agent_core


def test_phase_specific_events_include_budgets_and_attempts():
    sid = "phase3-visibility"
    agent_core.reset_session(sid)
    state = agent_core._begin_chat_run(sid)
    state["current_round"] = 1
    state["tool_attempts"] = 1

    lint = agent_core._tool_progress_event(
        "write_and_check_syntax", '{"ok": true}', args={}, state=state,
        round_no=1)
    simulation = agent_core._tool_progress_event(
        "run_simulation", "SIMULATION PASSED", args={}, state=state,
        round_no=1)
    synthesis = agent_core._tool_progress_event(
        "run_silicon_compiler", "", started=True,
        args={"clock_period_ns": 20}, state=state, round_no=1)

    assert lint["content"]["action"] == "Lint passed"
    assert simulation["content"]["action"] == "Simulation passed"
    assert synthesis["content"]["action"] == "Synthesizing at 50 MHz"
    for event in (lint, simulation, synthesis):
        assert event["content"]["attempts"]["tool"] == 1
        assert event["content"]["budgets"]["tokens"]["limit"] == agent_core.CHAT_TOKEN_BUDGET
        assert event["content"]["token_budget"] == event["content"]["budgets"]["tokens"]
    agent_core.reset_session(sid)


def test_rejected_revision_event_explains_slack_delta():
    sid = "phase3-visibility-reject"
    agent_core.reset_session(sid)
    args = {"verilog_code": "module m; endmodule", "testbench_code": "tb", "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "run_simulation", args, "SIMULATION PASSED")
    agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", args,
        "Synthesis successful!\ncellarea: 100\nsetupslack: 1.00")
    candidate = {**args, "verilog_code": "module m; wire x; endmodule", "flow": "syn-sta"}
    agent_core._postprocess_tool_result(sid, "run_simulation", candidate, "SIMULATION PASSED")
    result = agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", candidate,
        "Synthesis successful!\ncellarea: 1\nsetupslack: 0.33")

    event = agent_core._decision_event_for_result(
        "run_silicon_compiler", result, state=agent_core._get_state(sid), round_no=1)
    assert event["content"]["action"] == "Revision rejected: slack regressed by 0.67 ns"
    assert event["content"]["metric_delta"]["setupslack"] < 0
    assert event["content"]["attempts"]["non_improving"] == 1
    agent_core.reset_session(sid)


def test_budget_is_run_scoped_and_tool_limit_is_not_fabricated():
    sid = "phase3-visibility-budget"
    agent_core.reset_session(sid)
    state = agent_core._begin_chat_run(sid)
    agent_core._record_usage(sid, {"prompt_tokens": 100, "completion_tokens": 20,
                                   "total_tokens": 120})
    state["chat_token_start"] = state["tokens_used"]
    agent_core._record_usage(sid, {"prompt_tokens": 3, "completion_tokens": 2,
                                   "total_tokens": 5})
    state["next_prompt_estimate"] = 123
    budgets = agent_core._event_budgets(state, round_no=2)
    assert budgets["tokens"]["used"] == 5
    assert budgets["context"] == {
        "estimate": 123,
        "soft_limit": agent_core.CONTEXT_SOFT_LIMIT,
        "hard_limit": agent_core.CONTEXT_HARD_LIMIT,
        "hard_exceeded": False,
    }
    assert budgets["tools"]["used"] == 0
    assert budgets["tools"]["limit"] is None
    assert budgets["rounds"] == {"used": 2, "limit": agent_core.MAX_TOOL_ROUNDS,
                                  "remaining": agent_core.MAX_TOOL_ROUNDS - 2}
    agent_core.reset_session(sid)


def test_planning_is_hash_driven_and_duplicate_has_no_tool_start(monkeypatch):
    sid = "phase3-visibility-planning"
    code = "module m; endmodule"

    def call(call_id="c1"):
        fn = types.SimpleNamespace(name="write_and_check_syntax",
                                   arguments=json.dumps({"verilog_code": code,
                                                         "top_module": "m"}))
        return types.SimpleNamespace(id=call_id, function=fn)

    responses = iter([
        types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content=None, tool_calls=[call("c1")],
                model_dump=lambda exclude_none=True: {"role": "assistant", "tool_calls": []}))],
            usage=None),
        types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content=None, tool_calls=[call("c2")],
                model_dump=lambda exclude_none=True: {"role": "assistant", "tool_calls": []}))],
            usage=None),
        types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content="done", tool_calls=None,
                model_dump=lambda exclude_none=True: {"role": "assistant", "content": "done"}))],
            usage=None),
    ])

    async def create(**kwargs):
        return next(responses)

    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "fake"))
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "write_and_check_syntax",
                        lambda **kwargs: '{"ok": true}')
    try:
        events = asyncio.run(_collect(agent_core.chat("lint this", sid)))
    finally:
        agent_core.reset_session(sid)

    plans = [e for e in events if e["type"] == "progress" and
             e["content"].get("action", "").startswith("Planning revision")]
    starts = [e for e in events if e["type"] == "progress" and
              e["content"].get("action") == "Linting"]
    assert [e["content"]["action"] for e in plans] == ["Planning revision 1"]
    assert len(starts) == 1
    assert not any(e["type"] == "progress" and
                   e["content"].get("action") == "Planning revision 2"
                   for e in events)


async def _collect(generator):
    return [event async for event in generator]
