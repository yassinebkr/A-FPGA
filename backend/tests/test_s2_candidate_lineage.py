"""Offline S2 regressions for authoritative candidate lineage."""

import json

import agent_core


def _contract():
    return {
        "schema_version": "architecture/v1", "target_type": "asic",
        "top_module": "m", "technology": "sky130", "clock_target_mhz": 50,
        "clock_period_ns": 20, "latency_cycles": 2,
        "throughput_per_cycle": 1, "pipeline_stages": 1,
        "multiplier_strategy": "inferred", "memory_strategy": "registers",
    }


def _sim(sid, code, tb="tb-v1"):
    args = {"verilog_code": code, "testbench_code": tb, "top_module": "m"}
    agent_core._postprocess_tool_result(sid, "run_simulation", args,
                                        "SIMULATION PASSED (fixture)")
    return args


def _syn(sid, args, slack, *, flow=None, target="skywater130_demo", job="syn-fixture"):
    call = {**args, "target": target, "clock_period_ns": 20}
    if flow is not None:
        call["flow"] = flow
    result = ("Synthesis successful (fixture)\n"
              f"setupslack: {slack}\nfmax_mhz: {1000 / (20 - slack):.3f}\n"
              "toolchain: yosys 0.48; opensta 3.1\n"
              f"job_id: {job}")
    return agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", call, result)


def test_improvement_then_regression_restores_best_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    sid = "proj-s2-lineage"
    agent_core.reset_session(sid)
    agent_core._get_history(sid)
    state = agent_core._get_state(sid)
    state["controller_run_id"] = "run-s2-fixture"
    state["architecture_contract"] = _contract()
    state["architecture_contract_hash"] = agent_core._architecture_contract_hash(_contract())

    baseline = _sim(sid, "module m(input clk); always @(posedge clk) begin end endmodule")
    assert "REVISION ACCEPTED" in _syn(sid, baseline, 0.1, job="syn-base")
    improved = _sim(sid, "module m(input clk); reg x; always @(posedge clk) x<=0; endmodule")
    assert "REVISION ACCEPTED" in _syn(sid, improved, 0.8, job="syn-best")
    regressed = _sim(sid, "module m(input clk); reg [7:0] x; always @(posedge clk) x<=x+1; endmodule")
    assert "REVISION REJECTED" in _syn(sid, regressed, -0.2, job="syn-worse")

    best_hash = agent_core._code_sha256(improved["verilog_code"])
    worse_hash = agent_core._code_sha256(regressed["verilog_code"])
    assert state["best_revision"]["code_sha256"] == best_hash
    assert state["active_revision"]["code_sha256"] == best_hash
    assert state["latest_attempted_revision"]["code_sha256"] == worse_hash
    assert state["latest_attempted_revision"]["status"] == "rejected_regression"
    assert state["latest_attempted_revision"]["run_id"] == "run-s2-fixture"
    agent_core._save_project(sid)

    agent_core._session_state.pop(sid, None)
    agent_core._histories.pop(sid, None)
    agent_core._get_history(sid)
    restored = agent_core._get_state(sid)
    assert restored["best_revision"]["code_sha256"] == best_hash
    assert restored["active_revision"]["code_sha256"] == best_hash
    assert restored["latest_attempted_revision"]["code_sha256"] == worse_hash
    dashboard = agent_core.get_project_state(sid)
    assert dashboard["verilog"]["code"] == improved["verilog_code"]
    assert dashboard["metrics"] == restored["best_revision"]["metrics"]
    assert dashboard["candidate_lineage"]["latest_attempt"]["job_id"] == "syn-worse"
    agent_core.reset_session(sid)


def test_incompatible_measurement_cannot_replace_best():
    sid = "s2-incompatible"
    agent_core.reset_session(sid)
    state = agent_core._get_state(sid)
    state["controller_run_id"] = "run-s2-incompatible"
    state["architecture_contract"] = _contract()
    state["architecture_contract_hash"] = agent_core._architecture_contract_hash(_contract())
    first = _sim(sid, "module m(input clk); always @(posedge clk) begin end endmodule")
    assert "REVISION ACCEPTED" in _syn(sid, first, 0.1)
    best_hash = state["best_revision"]["code_sha256"]

    other = _sim(sid, "module m(input clk); reg y; always @(posedge clk) y<=0; endmodule",
                 tb="different-reference")
    assert "REVISION REJECTED" in _syn(sid, other, 5.0)
    assert state["best_revision"]["code_sha256"] == best_hash
    assert state["latest_attempted_revision"]["status"] == "rejected_incompatible"
    agent_core.reset_session(sid)


def test_lineage_rejects_artifact_placeholder_as_source():
    marker = "[artifact rtl:0123456789abcdef omitted from protocol tail]"
    value = {"schema_version": "candidate-lineage/v1", "revisions": [{
        "code_sha256": agent_core._code_sha256(marker), "verilog_code": marker,
    }]}
    assert agent_core._validated_candidate_lineage(value)["revisions"] == []


def test_exact_locked_approval_retry_is_idempotent(tmp_path, monkeypatch):
    from evidence import DesignSpec
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    sid = "proj-s2-idempotent"
    agent_core._get_history(sid)
    proposal = agent_core.extract_design_contract(sid, DesignSpec(
        design_id="m", name="m", top_module="m", target="sky130"))
    locked = agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=proposal["revision"], expected_digest=proposal["digest"])
    repeated = agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=locked["revision"], expected_digest=locked["digest"])
    assert repeated["revision"] == locked["revision"]
    assert repeated["digest"] == locked["digest"]
    agent_core.reset_session(sid)
