import json

import pytest

import agent_core
from timing_intent import validate_timing_retry_intent


def _checkpoint(sid: str, code: str) -> None:
    agent_core._postprocess_tool_result(
        sid,
        "run_simulation",
        {"verilog_code": code, "testbench_code": "module tb; endmodule", "top_module": "m"},
        "SIMULATION PASSED",
    )


def _record_failure(sid: str, code: str, diagnosis: dict) -> None:
    agent_core._postprocess_tool_result(
        sid,
        "run_silicon_compiler",
        {"verilog_code": code, "top_module": "m", "flow": "syn-sta"},
        "Synthesis successful!\ntiming_diagnosis: " + json.dumps(diagnosis),
    )


@pytest.fixture
def sid(request):
    value = "s1-" + request.node.name
    agent_core.reset_session(value)
    agent_core._begin_chat_run(value)
    yield value
    agent_core.reset_session(value)


def test_stale_unknown_evidence_allows_one_exact_verified_measurement(sid):
    old = "module m; wire old_path; endmodule"
    new = "module m; wire new_candidate; endmodule"
    _checkpoint(sid, old)
    _record_failure(sid, old, {
        "setup_slack_ns": -3.63,
        "paths": [{"startpoint": "pp02_reg/Q", "endpoint": "qhi0_reg/D", "slack_ns": -3.63}],
    })
    assert agent_core._get_state(sid)["timing_diagnosis"]["critical_path_classification"]["category"] == "unknown"
    _checkpoint(sid, new)

    args = {"verilog_code": new, "top_module": "m", "flow": "syn-sta"}
    assert agent_core._before_tool_call(sid, "run_silicon_compiler", args) is None
    assert args["_timing_diagnostic"]["reason_code"] == "stale_evidence"
    state = agent_core._get_state(sid)
    assert state["synthesis_invocations"] == 1
    assert len(state["timing_diagnostic_measurements"]) == 1

    duplicate = {"verilog_code": new, "top_module": "m", "flow": "syn-sta"}
    blocked = agent_core._before_tool_call(sid, "run_silicon_compiler", duplicate)
    assert "reason_code=diagnostic_allowance_exhausted" in blocked
    assert state["synthesis_invocations"] == 1


@pytest.mark.parametrize("tool_name", ["run_silicon_compiler", "find_fmax"])
def test_both_entry_points_share_unknown_diagnosis_terminal_policy(sid, tool_name):
    code = "module m; wire opaque_path; endmodule"
    _checkpoint(sid, code)
    _record_failure(sid, code, {
        "setup_slack_ns": -0.2,
        "paths": [{"startpoint": "u0/Q", "endpoint": "u1/D", "slack_ns": -0.2}],
    })
    before = agent_core._get_state(sid)["synthesis_invocations"]
    blocked = agent_core._before_tool_call(
        sid, tool_name, {"verilog_code": code, "top_module": "m"})
    assert "reason_code=diagnosis_inconclusive" in blocked
    assert agent_core._get_state(sid)["synthesis_invocations"] == before


def test_intent_reasons_distinguish_missing_endpoint_and_unsupported():
    diagnosis = {
        "setup_slack_ns": -1.0,
        "paths": [{"startpoint": "u_mul/Q", "endpoint": "u_acc/D", "slack_ns": -1.0}],
    }
    missing = validate_timing_retry_intent(
        None, diagnosis, require_supported_strategy=True)
    assert missing["reason_code"] == "missing_intent"

    endpoint = validate_timing_retry_intent({
        "strategy": "register_product", "rationale": "split MAC",
        "expected_change": "shorter cone", "target_startpoint": "wrong/Q",
        "target_endpoint": "u_acc/D",
    }, diagnosis, require_supported_strategy=True)
    assert endpoint["reason_code"] == "endpoint_mismatch"

    unsupported = validate_timing_retry_intent({
        "strategy": "reduce_fanout", "rationale": "try another transform",
        "expected_change": "shorter cone", "target_startpoint": "u_mul/Q",
        "target_endpoint": "u_acc/D",
    }, diagnosis, require_supported_strategy=True)
    assert unsupported["reason_code"] == "unsupported_strategy"

    valid = validate_timing_retry_intent({
        "strategy": "register_product", "rationale": "split MAC",
        "expected_change": "shorter cone", "target_startpoint": "u_mul/Q",
        "target_endpoint": "u_acc/D",
    }, diagnosis, require_supported_strategy=True)
    assert valid["ok"] is True
    assert valid["reason_code"] == "accepted"


def test_invalid_preflight_is_bounded_without_consuming_execution_count(sid):
    code = "module m; wire mul; wire acc; endmodule"
    _checkpoint(sid, code)
    _record_failure(sid, code, {
        "setup_slack_ns": -1.0,
        "paths": [{"startpoint": "mul_reg/Q", "endpoint": "acc_reg/D", "slack_ns": -1.0}],
    })
    state = agent_core._get_state(sid)
    baseline = state["synthesis_invocations"]
    for attempt in range(agent_core.MAX_TIMING_ADMISSION_REJECTIONS):
        blocked = agent_core._before_tool_call(
            sid, "run_silicon_compiler", {"verilog_code": code, "top_module": "m"})
        assert "reason_code=missing_intent" in blocked
        if attempt + 1 == agent_core.MAX_TIMING_ADMISSION_REJECTIONS:
            assert "Rejection guard exhausted" in blocked
    assert state["synthesis_invocations"] == baseline
    assert "timing_admission_rejections_exhausted" in state["run_stop_reason"]


def test_inconclusive_admission_result_is_not_recorded_as_synthesis(sid):
    code = "module m; wire opaque_path; endmodule"
    _checkpoint(sid, code)
    state = agent_core._get_state(sid)
    revision = state["revision_by_hash"][agent_core._code_sha256(code)]
    before_attempts = list(revision.get("synthesis_attempts") or [])
    result = (
        "Timing retry stopped: measured critical path remains unclassified. "
        "[reason_code=diagnosis_inconclusive] No synthesis was started."
    )
    processed = agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler",
        {"verilog_code": code, "top_module": "m", "flow": "syn-sta"}, result)
    revision = state["revision_by_hash"][agent_core._code_sha256(code)]
    assert revision.get("synthesis_attempts", []) == before_attempts
    assert "reason_code=diagnosis_inconclusive" in processed


def test_toolchain_preflight_failure_has_distinct_reason(sid):
    code = "module m; endmodule"
    _checkpoint(sid, code)
    processed = agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler",
        {"verilog_code": code, "top_module": "m", "flow": "syn-sta"},
        "Synthesis failed. Version check failed for yosys; "
        "tools did not meet version requirements",
    )
    assert "reason_code=infrastructure_failure" in processed
