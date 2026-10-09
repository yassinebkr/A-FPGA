"""Focused contract tests for Phase 3 screen-first implementation policy."""

import agent_core


def _sim(sid, code):
    args = {"verilog_code": code, "testbench_code": "tb", "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "run_simulation", args, "SIMULATION PASSED")
    return args


def _screen(sid, args, slack="0.50", area="100"):
    syn = {**args, "flow": "syn-sta"}
    out = agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", syn,
        f"Synthesis successful (synthesis + static timing)!\ncellarea: {area}\nsetupslack: {slack}",
    )
    return syn, out


def test_full_pnr_requires_a_promoted_cheap_screen():
    sid = "phase3-pnr-guard"
    agent_core.reset_session(sid)
    args = _sim(sid, "module m; endmodule")
    blocked = agent_core._before_tool_call(
        sid, "run_silicon_compiler", {**args, "flow": "full-pnr"}
    )
    assert blocked and "promotion_token" in blocked
    assert agent_core._get_state(sid)["full_pnr_invocations"] == 0
    agent_core.reset_session(sid)


def test_screen_promotes_then_allows_only_one_full_pnr_run():
    sid = "phase3-promote-once"
    agent_core.reset_session(sid)
    args = _sim(sid, "module m; wire x; endmodule")
    screen, out = _screen(sid, args)
    assert "CANDIDATE PROMOTED" in out
    state = agent_core._get_state(sid)
    assert state["promoted_revision"]["code_sha256"] == agent_core._code_sha256(args["verilog_code"])

    full_without_token = {**screen, "flow": "full"}
    omitted = agent_core._before_tool_call(
        sid, "run_silicon_compiler", full_without_token
    )
    assert omitted and "promotion_token" in omitted
    full = {**full_without_token,
            "promotion_token": state["promoted_revision"]["promotion_token"]}
    assert agent_core._before_tool_call(sid, "run_silicon_compiler", full) is None
    pnr = agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", full,
        "Synthesis successful (full ASIC flow)!\ncellarea: 100\nsetupslack: 0.60",
    )
    assert "REVISION ACCEPTED" in pnr
    assert agent_core._get_state(sid)["full_pnr_invocations"] == 1

    repeated = agent_core._before_tool_call(sid, "run_silicon_compiler", full)
    assert repeated and ("already" in repeated.lower() or "experiment" in repeated.lower())
    agent_core.reset_session(sid)


def test_screen_regression_does_not_displace_best_checkpoint():
    sid = "phase3-screen-reject"
    agent_core.reset_session(sid)
    good = _sim(sid, "module m; wire good; endmodule")
    # Establish a final baseline through the legacy-compatible measured path.
    agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", good,
        "Synthesis successful!\ncellarea: 100\nsetupslack: 0.50",
    )
    bad = _sim(sid, "module m; wire bad; endmodule")
    _, out = _screen(sid, bad, slack="0.10", area="1")
    assert "REVISION REJECTED" in out
    assert agent_core._get_state(sid)["best_revision"]["verilog_code"] == good["verilog_code"]
    agent_core.reset_session(sid)


def test_staged_screen_regression_keeps_best_and_allows_full_pnr():
    sid = "phase3-staged-screen-budget"
    agent_core.reset_session(sid)
    candidates = [
        ("module m; wire first; endmodule", "0.50", "100"),
        ("module m; wire best; endmodule", "0.60", "100"),
        ("module m; wire regression; endmodule", "0.40", "1"),
    ]
    args_by_code = {}
    outputs = []
    for code, slack, area in candidates:
        args = _sim(sid, code)
        args_by_code[code] = args
        screened = {**args, "flow": "syn-sta"}
        assert agent_core._before_tool_call(sid, "run_silicon_compiler", screened) is None
        outputs.append(agent_core._postprocess_tool_result(
            sid, "run_silicon_compiler", screened,
            f"Synthesis successful (synthesis + static timing)!\n"
            f"cellarea: {area}\nsetupslack: {slack}"))

    state = agent_core._get_state(sid)
    assert state["screen_attempts"] == 3
    assert state["screen_best_revision"]["verilog_code"] == candidates[1][0]
    assert state["promoted_revision"]["verilog_code"] == candidates[1][0]
    assert "CHEAP SCREEN LIMIT REACHED" in outputs[-1]
    assert state["run_stop_reason"] is None

    promoted = state["promoted_revision"]
    full = {**args_by_code[candidates[1][0]], "flow": "full-pnr",
            "promotion_token": promoted["promotion_token"]}
    assert agent_core._before_tool_call(sid, "run_silicon_compiler", full) is None
    assert state["full_pnr_invocations"] == 1
    assert state["synthesis_invocations"] == 4
    agent_core.reset_session(sid)
