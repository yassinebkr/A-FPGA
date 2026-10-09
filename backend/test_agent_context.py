"""Focused tests for bounded model-facing context construction."""
import copy
import hashlib
import json

import agent_core


def _history(rtl, tb, result="SIMULATION FAILED: mismatch"):
    args = {"verilog_code": rtl, "testbench_code": tb, "top_module": "dut"}
    return [
        {"role": "system", "content": "old persisted system prompt"},
        {"role": "user", "content": "Design and verify the DUT."},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "run_simulation",
                    "arguments": json.dumps(args),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": result},
    ]


def test_context_is_bounded_and_keeps_latest_artifacts_and_diagnostic():
    rtl = "module dut;\n" + ("wire [7:0] data;\n" * 5000) + "endmodule"
    tb = "module tb;\ninitial $display(\"FAIL\");\nendmodule"
    history = _history(rtl, tb, json.dumps({
        "ok": False,
        "verdict": "syntax_error",
        "errors_total": 1,
        "diagnostics": [{"line": 4, "msg": "expected ';'"}],
    }))
    before = copy.deepcopy(history)

    context = agent_core.build_model_context(history, {})
    encoded = json.dumps(context)
    assert len(encoded) <= agent_core.MODEL_CONTEXT_MAX_CHARS
    assert history == before  # compaction must never mutate durable history
    dynamic = context[1]["content"]
    assert "CURRENT ARTIFACTS" in dynamic
    assert hashlib.sha256(rtl.encode()).hexdigest()[:16] in dynamic
    assert "syntax_error" in dynamic
    assert "expected ';'" in dynamic
    assert any(m.get("role") == "user" and "Design and verify" in m.get("content", "")
               for m in context)


def test_context_preserves_current_tool_protocol_and_compacts_source_args():
    rtl = "module dut;\n" + ("assign out = in;\n" * 200) + "endmodule"
    context = agent_core.build_model_context(_history(rtl, "module tb; endmodule"), {})
    roles = [m["role"] for m in context]
    # The active exchange stays ordered as assistant tool_calls -> tool result.
    assistant_index = roles.index("assistant")
    assert roles[assistant_index + 1] == "tool"
    call = context[assistant_index]["tool_calls"][0]
    compact_args = json.loads(call["function"]["arguments"])
    assert compact_args["verilog_code"].startswith("[artifact rtl:")
    assert context[assistant_index + 1]["tool_call_id"] == call["id"]


def test_latest_rtl_and_testbench_win_over_older_attempts():
    history = _history("module old; endmodule", "module old_tb; endmodule")
    newer = {"verilog_code": "module new; endmodule",
             "testbench_code": "module new_tb; endmodule",
             "top_module": "new"}
    history.append({"role": "assistant", "tool_calls": [{
        "id": "call-new", "type": "function",
        "function": {"name": "write_and_check_syntax", "arguments": json.dumps(newer)},
    }]})
    context = agent_core.build_model_context(history, {})
    dynamic = context[1]["content"]
    assert "module new; endmodule" in dynamic
    assert "module new_tb; endmodule" in dynamic
    assert "top=new" in dynamic


def test_reasoning_heavy_assistant_is_stripped_and_protocol_remains_valid():
    reasoning = "private reasoning " * 30_000
    history = [
        {"role": "system", "content": "old system", "provider_trace": "x"},
        {"role": "user", "content": "Build and verify the DUT."},
        {
            "role": "assistant",
            "content": None,
            "reasoning": reasoning,
            "reasoning_content": reasoning,
            "tool_calls": [{
                "id": "call-heavy", "type": "function", "provider_meta": "drop",
                "function": {"name": "run_simulation",
                             "arguments": '{"verilog_code":"module dut; endmodule", "top_module":"dut"}',
                             "reasoning": reasoning},
            }],
        },
        {"role": "tool", "tool_call_id": "call-heavy", "content": "SIMULATION PASSED",
         "reasoning": reasoning},
    ]

    durable = agent_core._sanitize_history(history)
    assert "reasoning" not in json.dumps(durable)
    assert "reasoning_content" not in json.dumps(durable)
    assert durable[2]["tool_calls"][0]["function"]["name"] == "run_simulation"

    context = agent_core.build_model_context(history, {})
    encoded = json.dumps(context)
    assert "reasoning" not in encoded
    assert "reasoning_content" not in encoded
    assert all(set(message) <= {"role", "content", "name", "tool_calls", "refusal",
                                "tool_call_id"} for message in context)
    roles = [message["role"] for message in context]
    assert roles.count("system") >= 1
    assert "user" in roles
    assert roles[-2:] == ["assistant", "tool"]
    assert all(str(message.get("content") or "").strip() for message in context
               if message["role"] in {"system", "user", "tool"})


def test_context_fitter_never_erases_protected_messages(monkeypatch):
    monkeypatch.setattr(agent_core, "MODEL_CONTEXT_MAX_CHARS", 256)
    history = _history("module dut; endmodule", "module tb; endmodule", "tool output")
    context = agent_core.build_model_context(history, {})
    roles = [message["role"] for message in context]
    assert roles[0] == "system" and context[0]["content"]
    assert "user" in roles and next(message for message in context
                                     if message["role"] == "user")["content"]
    assistant = next(message for message in context if message["role"] == "assistant")
    tool = next(message for message in context if message["role"] == "tool")
    assert assistant["tool_calls"] and tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
