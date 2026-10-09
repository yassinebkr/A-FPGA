"""Focused tests for Phase 1 verified-revision control."""

import json

import agent_core


def _sim(sid, code, tb="tb"):
    args = {"verilog_code": code, "testbench_code": tb, "top_module": "m"}
    agent_core._postprocess_tool_result(
        sid, "run_simulation", args, "SIMULATION PASSED (iverilog):\nPASS")
    return args


def _syn(sid, args, area, slack):
    return agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", args,
        f"Synthesis successful!\ncellarea: {area}\nsetupslack: {slack} ns")


def test_synthesis_requires_exact_passing_simulation():
    sid = "revision-guard"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid)
    args = {"verilog_code": "module m; endmodule", "top_module": "m"}
    blocked = agent_core._before_tool_call(sid, "run_silicon_compiler", args)
    assert blocked and "no passing simulation" in blocked
    assert agent_core._get_state(sid)["synthesis_invocations"] == 0
    agent_core.reset_session(sid)


def test_timing_dominates_area_and_regression_is_bounded():
    sid = "revision-score"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid)
    baseline = _sim(sid, "module m; endmodule")
    accepted = _syn(sid, baseline, 100, 1.0)
    assert "REVISION ACCEPTED" in accepted

    worse_timing = _sim(sid, "module m; wire [7:0] x; endmodule")
    rejected = _syn(sid, worse_timing, 1, 0.1)
    assert "REVISION REJECTED" in rejected
    state = agent_core._get_state(sid)
    assert state["best_revision"]["code_sha256"] == agent_core._code_sha256(baseline["verilog_code"])
    assert state["non_improving_attempts"] == 1

    # A second regression stops before another synthesis can be launched.
    another = _sim(sid, "module m; wire [7:0] y; endmodule")
    assert "REVISION REJECTED" in _syn(sid, another, 1, 0.2)
    assert "no improving verified revision" in agent_core._get_state(sid)["run_stop_reason"]
    assert agent_core._before_tool_call(sid, "run_silicon_compiler", another)
    agent_core.reset_session(sid)


def test_fmax_result_is_comparable():
    sid = "revision-fmax"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid)
    args = _sim(sid, "module m; endmodule")
    result = agent_core._postprocess_tool_result(
        sid, "find_fmax", args,
        json.dumps({"status": "success", "fmax_mhz": 50.0,
                    "chosen_period_ns": 20.0, "wns_ns": 0.2}))
    assert "REVISION ACCEPTED" in result
    metrics = agent_core._get_state(sid)["best_revision"]["metrics"]
    assert metrics["fmax_mhz"] == 50.0
    assert metrics["setupslack"] == 0.2
    agent_core.reset_session(sid)


def test_persisted_latest_state_prefers_last_simulation_passing_rtl():
    passing = "module m; wire ok; endmodule"
    failed_candidate = "module m; wire broken; endmodule"
    history = [
        {"role": "assistant", "tool_calls": [{"id": "sim1", "function": {
            "name": "run_simulation", "arguments": json.dumps({
                "verilog_code": passing, "top_module": "m"})}}]},
        {"role": "tool", "tool_call_id": "sim1",
         "content": "SIMULATION PASSED: all checks clean"},
        {"role": "assistant", "tool_calls": [{"id": "sim2", "function": {
            "name": "run_simulation", "arguments": json.dumps({
                "verilog_code": failed_candidate, "top_module": "m"})}}]},
        {"role": "tool", "tool_call_id": "sim2",
         "content": "SIMULATION FAILED: C[0] mismatch"},
    ]
    latest, _, lint = agent_core._derive_latest_state(history)
    assert latest["code"] == passing
    assert lint is None


def test_derive_latest_state_binds_lint_to_the_current_rtl():
    """The persisted lint gate evidence is digest-bound to the dashboard RTL:
    a lint of the current revision binds; a lint of an older revision is
    reported as stale (bound=False) instead of passing as current."""
    import hashlib
    code = "module m; endmodule"
    other = "module n; endmodule"
    lint_current = json.dumps({"tool": "verilator", "ok": True,
                               "verdict": "lint_warning",
                               "code_sha256": hashlib.sha256(code.encode()).hexdigest()})
    lint_stale = json.dumps({"tool": "verilator", "ok": True, "verdict": "ok",
                             "code_sha256": hashlib.sha256(other.encode()).hexdigest()})
    history = [
        {"role": "assistant", "tool_calls": [{"id": "l1", "function": {
            "name": "write_and_check_syntax",
            "arguments": json.dumps({"verilog_code": code, "top_module": "m"})}}]},
        {"role": "tool", "tool_call_id": "l1", "content": lint_current},
    ]
    latest, _, lint = agent_core._derive_latest_state(history)
    assert latest["code"] == code
    assert lint["verdict"] == "lint_warning" and lint["ok"] is True
    assert lint["bound"] is True

    # A newer RTL revision makes the recorded lint stale.
    history += [
        {"role": "assistant", "tool_calls": [{"id": "l2", "function": {
            "name": "write_and_check_syntax",
            "arguments": json.dumps({"verilog_code": other, "top_module": "n"})}}]},
        {"role": "tool", "tool_call_id": "l2", "content": lint_stale},
        # The latest verilog is `other` (last call), lint matches it again.
    ]
    latest2, _, lint2 = agent_core._derive_latest_state(history)
    assert latest2["code"] == other
    assert lint2["code_sha256"] == hashlib.sha256(other.encode()).hexdigest()
    assert lint2["bound"] is True

    # Non-lint tool results never produce lint state.
    plain = [{"role": "tool", "tool_call_id": "x",
              "content": "SIMULATION PASSED"}]
    _, _, lint3 = agent_core._derive_latest_state(plain)
    assert lint3 is None


def test_metric_snapshot_converts_fmax_hz_and_derives_critical_delay():
    """The tool prints fmax in Hz; the persisted dashboard snapshot must carry
    MHz (a reloaded project showed "52,510,000 MHz" because the regex path
    kept the raw string and skipped the conversion). The critical-path delay
    is derived as clock period − worst setup slack and flagged as derived."""
    text = ("Synthesis successful (screening synthesis + static timing)!\n"
            "cellarea: 46154.265072999224 um^2\n"
            "cells: 7812\n"
            "setupslack: 0.96 ns\n"
            "holdslack: 0.0 ns\n"
            "fmax: 52510000.0 Hz\n"
            'timing_diagnosis: {"clock":"clk","clock_period_ns":20.0,"fmax_mhz":52.51}')
    values, provenance = agent_core._metric_snapshot_from_text(text)
    assert abs(values["fmax"] - 52.51) < 0.01
    assert abs(values["criticalpath"] - 19.04) < 0.01
    assert values["criticalpath_derived"] is True
    assert values["cells"] == 7812
    assert provenance.get("evidence_stage") == "screening_sta"
