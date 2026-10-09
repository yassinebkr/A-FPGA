import json

import agent_core


def test_negative_timing_requires_intent_and_json_diagnosis_is_preserved():
    sid = "phase1-timing-intent-controller"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid)
    code = "module m; endmodule"
    sim = {"verilog_code": code, "testbench_code": "tb", "top_module": "m"}
    syn = {"verilog_code": code, "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "run_simulation", sim, "SIMULATION PASSED")
    diagnosis = {"setup_slack_ns": -1.0, "paths": [{"startpoint": "u_mul/a", "endpoint": "u_acc/d"}]}
    agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", syn,
        "Synthesis successful!\ntiming_diagnosis: " + json.dumps(diagnosis))
    state = agent_core._get_state(sid)
    assert state["timing_diagnosis"]["setup_slack_ns"] == -1.0
    blocked = agent_core._before_tool_call(sid, "run_silicon_compiler", syn)
    assert blocked and "timing_intent" in blocked
    assert state["synthesis_invocations"] == 0
    syn["timing_intent"] = {
        "strategy": "register_product", "rationale": "register the product before accumulation",
        "expected_change": "one product register", "target_startpoint": "u_mul/a",
        "target_endpoint": "u_acc/d",
    }
    assert agent_core._before_tool_call(sid, "run_silicon_compiler", syn) is None
    assert state["synthesis_invocations"] == 1
    agent_core.reset_session(sid)
