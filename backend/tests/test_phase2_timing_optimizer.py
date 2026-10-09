import json

import agent_core
from timing_intent import validate_timing_retry_intent
from timing_optimizer import classify_timing_path, validate_strategy_for_diagnosis


def test_classifier_normalizes_synthesized_identifiers_and_recommends_mac_strategy():
    diagnosis = {
        "setup_slack_ns": -1.2,
        "paths": [{
            "startpoint": "pe0/r_prod_reg[0]",
            "endpoint": "pe0/acc_flat_reg[0]/D",
            "slack_ns": -1.2,
        }],
    }
    result = classify_timing_path(diagnosis)
    assert result["category"] == "multiplier_to_accumulator"
    assert result["confidence"] == "high"
    assert result["startpoint"] == "pe0/r_prod_reg[0]"
    assert "register_product" in result["recommended_strategies"]


def test_strategy_requires_measured_path_and_category_match():
    diagnosis = {
        "setup_slack_ns": -0.5,
        "paths": [{"startpoint": "u_mul/a", "endpoint": "u_acc/d", "slack_ns": -0.5}],
    }
    good = validate_strategy_for_diagnosis({
        "strategy": "register_product",
        "target_startpoint": "u_mul/a",
        "target_endpoint": "u_acc/d",
    }, diagnosis)
    assert good["ok"] is True
    bad = validate_strategy_for_diagnosis({
        "strategy": "reduce_fanout",
        "target_startpoint": "u_mul/a",
        "target_endpoint": "u_acc/d",
    }, diagnosis)
    assert bad["ok"] is False
    wrong_path = validate_strategy_for_diagnosis({
        "strategy": "register_product",
        "target_startpoint": "u_other/q",
        "target_endpoint": "u_acc/d",
    }, diagnosis)
    assert wrong_path["ok"] is False


def test_unknown_path_authorizes_no_speculative_strategy():
    diagnosis = {"setup_slack_ns": -0.2, "paths": [{
        "startpoint": "u0/q", "endpoint": "u1/d", "slack_ns": -0.2
    }]}
    result = classify_timing_path(diagnosis)
    assert result["category"] == "unknown"
    assert result["confidence"] == "low"
    rejected = validate_strategy_for_diagnosis({
        "strategy": "pipeline", "target_startpoint": "u0/q", "target_endpoint": "u1/d"
    }, diagnosis)
    assert rejected["ok"] is False
    assert rejected["allowed_strategies"] == []


def test_controller_rejects_repeated_timing_strategy_before_eda():
    sid = "phase2-repeated-strategy"
    agent_core.reset_session(sid)
    code = "module m; endmodule"
    args = {"verilog_code": code, "testbench_code": "tb", "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "run_simulation", args, "SIMULATION PASSED")
    syn = {"verilog_code": code, "top_module": "m"}
    diagnosis = {"setup_slack_ns": -1.0, "paths": [{"startpoint": "u_mul/a", "endpoint": "u_acc/d"}]}
    agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", syn,
        "Synthesis successful!\ntiming_diagnosis: " + json.dumps(diagnosis),
    )
    retry = {**syn, "timing_intent": {
        "strategy": "register_product", "rationale": "register the product before accumulation",
        "expected_change": "one product register", "target_startpoint": "u_mul/a",
        "target_endpoint": "u_acc/d",
    }}
    assert agent_core._before_tool_call(sid, "run_silicon_compiler", retry) is None
    blocked = agent_core._before_tool_call(sid, "run_silicon_compiler", retry)
    assert blocked and "already measured" in blocked
    assert agent_core._get_state(sid)["synthesis_invocations"] == 1
    agent_core.reset_session(sid)


def test_contract_timing_intent_preserves_latency_and_throughput():
    result = validate_timing_retry_intent(
        {"strategy": "pipeline_critical_path", "rationale": "split path", "expected_change": "register",
         "latency_cycles": 4, "throughput_per_cycle": 1},
        {"setup_slack_ns": -1, "paths": []}, require_path=False,
        require_supported_strategy=True,
        architecture_contract={"latency_cycles": 3, "throughput_per_cycle": 1},
    )
    assert result["ok"] is False
    assert "contract_preservation" in result["missing"]
