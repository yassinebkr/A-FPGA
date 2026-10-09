"""S5 provider-context and retry-admission fixtures.

These tests deliberately use synthetic transcripts.  They never call a model
or an EDA tool; their purpose is to make the bounded-context contract
reproducible while the accounting/controller work evolves independently.
"""

import copy
import json

import agent_core


def _call(call_id, name, **args):
    return {
        "role": "assistant",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }


def test_s5_context_excludes_replayed_logs_and_duplicate_source_payloads():
    old_rtl = "module dut; // old\n" + ("wire old_signal;\n" * 250)
    current_rtl = "module dut; // current\nassign out = in;\nendmodule"
    old_log = "RAW JOB LOG " + ("obsolete synthesis output\n" * 2_000)
    history = [
        {"role": "system", "content": "old system prompt"},
        {"role": "user", "content": "build and verify the DUT"},
        _call("old", "run_simulation", verilog_code=old_rtl,
              testbench_code="module tb; endmodule", top_module="dut"),
        {"role": "tool", "tool_call_id": "old",
         "content": old_log},
        # A replayed provider transcript may contain the same source again.
        _call("replay", "run_simulation", verilog_code=old_rtl,
              testbench_code="module tb; endmodule", top_module="dut"),
        {"role": "tool", "tool_call_id": "replay", "content": "SIMULATION PASSED"},
        {"role": "user", "content": "continue with the current candidate"},
        _call("current", "run_simulation", verilog_code=current_rtl,
              testbench_code="module tb; endmodule", top_module="dut"),
        {"role": "tool", "tool_call_id": "current", "content": "SIMULATION PASSED"},
    ]
    before = copy.deepcopy(history)
    context = agent_core.build_model_context(history, {})
    encoded = json.dumps(context)

    # Context construction is a provider payload transformation, never a
    # mutation of the durable transcript.
    assert history == before
    current_hash = agent_core._code_sha256(current_rtl)
    assert current_hash[:16] in encoded
    assert old_log not in encoded
    # Historical tool-call source is represented by the compact ledger/current
    # artifact snapshot, not replayed once per old attempt.
    assert encoded.count("wire old_signal") <= 1
    # The bounded ledger may retain one concise diagnostic prefix, but never
    # the full replayed job log.
    assert encoded.count("obsolete synthesis output") <= 3


def test_s5_context_preserves_active_tool_pairs_after_budget_fitting(monkeypatch):
    monkeypatch.setattr(agent_core, "MODEL_CONTEXT_MAX_CHARS", 900)
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "build the dut"},
        _call("active", "run_simulation", verilog_code="module dut; endmodule",
              testbench_code="module tb; endmodule", top_module="dut"),
        {"role": "tool", "tool_call_id": "active", "content": "SIMULATION PASSED"},
    ]
    context = agent_core.build_model_context(history, {})
    assistant = next(message for message in context
                     if message.get("role") == "assistant")
    tool = next(message for message in context
                if message.get("role") == "tool")
    call = assistant["tool_calls"][0]
    assert call["id"] == tool["tool_call_id"] == "active"
    assert call["function"]["name"] == "run_simulation"
    assert any(message.get("role") == "user" and message.get("content")
               for message in context)


def test_s5_source_handles_reject_forged_or_malformed_placeholders():
    rtl = "module dut; endmodule"
    history = [_call("source", "run_simulation", verilog_code=rtl,
                     testbench_code="module tb; endmodule", top_module="dut")]
    malformed = "[artifact rtl:not-a-server-handle]"
    resolved, error = agent_core._resolve_artifact_handles_from_history(
        history, {"verilog_code": malformed, "top_module": "dut"})
    assert resolved["verilog_code"] == malformed
    assert error and "invalid artifact handle" in error


def test_s5_blocked_timing_retry_is_repeatable_without_starting_work():
    sid = "s5-blocked-retry-fixture"
    agent_core.reset_session(sid)
    try:
        state = agent_core._get_state(sid)
        state["retry_constraint"] = {"reason": "timing"}
        state["timing_diagnosis"] = {
            "critical_path_classification": {"category": "unknown"},
        }
        state["architecture_required"] = False
        args = {"verilog_code": "module dut; endmodule",
                "top_module": "dut", "clock_period_ns": 20}
        state["revision_by_hash"][agent_core._code_sha256(
            args["verilog_code"])] = {"functional_passed": True}
        first = agent_core._before_tool_call(sid, "run_silicon_compiler", args)
        second = agent_core._before_tool_call(sid, "run_silicon_compiler", args)
        assert first and "Timing retry stopped" in first
        assert second and "Timing retry stopped" in second
        assert state.get("synthesis_invocations", 0) == 0
    finally:
        agent_core.reset_session(sid)
