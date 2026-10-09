"""Focused contract tests for the Phase 1 optimization controller.

These tests intentionally exercise the existing agent_core tool-result seam,
rather than starting SiliconCompiler.  A Phase 1 implementation must update
the per-session revision state only after a verified (simulation-passing)
design is measured, retain the best checkpoint when a later synthesis
regresses, and stop bounded optimization after two non-improving proposals.
"""

import agent_core


def _sim_args(code):
    return {
        "verilog_code": code,
        "testbench_code": "tb",
        "top_module": "matmul3",
    }


def _synth_args(code):
    return {"verilog_code": code, "top_module": "matmul3"}


def _synthesis_result(*, slack, area=100.0, path="u_mul/a -> u_acc/d"):
    # Keep the fixture close to the text emitted by the EDA tools while also
    # carrying the path diagnosis the retry policy must preserve.
    return (
        "Synthesis successful!\n"
        f"cellarea: {area}\n"
        f"setupslack: {slack}\n"
        f"critical_path: {path}"
    )


def _start(sid):
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid)


def test_simulation_pass_creates_verified_checkpoint_and_selects_best():
    sid = "phase1-checkpoint-selection"
    _start(sid)
    code = "module matmul3; endmodule"

    agent_core._postprocess_tool_result(
        sid, "run_simulation", _sim_args(code), "SIMULATION PASSED (iverilog):\nPASS"
    )
    agent_core._postprocess_tool_result(
        sid,
        "run_silicon_compiler",
        _synth_args(code),
        _synthesis_result(slack="0.25"),
    )

    state = agent_core._get_state(sid)
    assert state["revisions"], "a passing + measured RTL must be checkpointed"
    best = state["best_revision"]
    assert best["verilog_code"] == code
    assert float(best["setupslack_ns"]) == 0.25
    agent_core.reset_session(sid)


def test_regression_is_rejected_and_best_checkpoint_is_retained():
    sid = "phase1-regression-restore"
    _start(sid)
    good = "module matmul3; wire good; endmodule"
    bad = "module matmul3; wire bad; endmodule"

    for code, slack in ((good, "0.50"), (bad, "-0.25")):
        agent_core._postprocess_tool_result(
            sid, "run_simulation", _sim_args(code), "SIMULATION PASSED (iverilog):\nPASS"
        )
        out = agent_core._postprocess_tool_result(
            sid,
            "run_silicon_compiler",
            _synth_args(code),
            _synthesis_result(slack=slack),
        )

    state = agent_core._get_state(sid)
    assert state["best_revision"]["verilog_code"] == good
    assert state["best_revision"]["setupslack_ns"] == 0.50
    assert state["active_revision"]["verilog_code"] == good
    assert "regression" in out.lower() or "restor" in out.lower()
    agent_core.reset_session(sid)


def test_two_non_improving_attempts_stop_optimization():
    sid = "phase1-two-non-improving"
    _start(sid)
    base = "module matmul3; wire base; endmodule"
    candidate = "module matmul3; wire candidate; endmodule"

    agent_core._postprocess_tool_result(
        sid, "run_simulation", _sim_args(base), "SIMULATION PASSED (iverilog):\nPASS"
    )
    agent_core._postprocess_tool_result(
        sid,
        "run_silicon_compiler",
        _synth_args(base),
        _synthesis_result(slack="0.50"),
    )
    for _ in range(2):
        agent_core._postprocess_tool_result(
            sid, "run_simulation", _sim_args(candidate), "SIMULATION PASSED (iverilog):\nPASS"
        )
        out = agent_core._postprocess_tool_result(
            sid,
            "run_silicon_compiler",
            _synth_args(candidate),
            _synthesis_result(slack="0.25"),
        )

    state = agent_core._get_state(sid)
    assert state["non_improving_attempts"] == 2
    assert state["run_stop_reason"]
    assert "non-improv" in state["run_stop_reason"].lower()
    assert "non-improv" in out.lower()
    agent_core.reset_session(sid)


def test_timing_retry_must_carry_diagnosed_critical_path():
    sid = "phase1-diagnosed-path"
    _start(sid)
    code = "module matmul3; wire x; endmodule"
    path = "u_mul/a -> u_acc/d"
    agent_core._postprocess_tool_result(
        sid, "run_simulation", _sim_args(code), "SIMULATION PASSED (iverilog):\nPASS"
    )
    agent_core._postprocess_tool_result(
        sid,
        "run_silicon_compiler",
        _synth_args(code),
        _synthesis_result(slack="-1.0", path=path),
    )

    state = agent_core._get_state(sid)
    diagnosis = state.get("timing_diagnosis")
    assert diagnosis, "the failed path must be available to guide the next proposal"
    assert path in str(diagnosis)
    assert state.get("retry_constraint")
    assert path in str(state["retry_constraint"])
    agent_core.reset_session(sid)
