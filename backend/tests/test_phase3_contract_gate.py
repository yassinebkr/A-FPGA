"""Focused Phase 3 contract-controller tests."""

import json

import agent_core
from architecture_contract import define_architecture


def _architecture(**changes):
    value = {
        "target_type": "asic", "top_module": "dut", "technology": "sky130",
        "clock_target_mhz": 50, "clock_period_ns": 20, "latency_cycles": 2,
        "throughput_per_cycle": 1, "pipeline_stages": 1,
        "multiplier_strategy": "inferred", "memory_strategy": "registers",
    }
    value.update(changes)
    return value


def _proposal(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    agent_core._project_repository = None
    sid = "proj-phase3"
    agent_core.reset_session(sid)
    agent_core._get_history(sid)
    payload = _architecture()
    raw = define_architecture(**payload)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload, raw)
    surface = agent_core.get_design_contract(sid)
    state = agent_core._get_state(sid)
    contract = agent_core._contract_state_from(surface["contract"])
    contract.pending_request = True
    contract.paused_reason = "awaiting_contract_approval"
    contract.digest = contract.recompute_digest()
    state["design_contract"] = contract.canonical()
    state["pending_request"] = "build dut"
    return sid, agent_core.get_design_contract(sid)


def test_approve_and_build_binds_exact_revision_and_allows_lint(monkeypatch, tmp_path):
    sid, proposal = _proposal(monkeypatch, tmp_path)
    approved = agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=proposal["revision"], expected_digest=proposal["digest"])
    assert approved["status"] == "locked"
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    state = agent_core._get_state(sid)
    assert state["contract_run_digest"] == state["design_contract"]["digest"]
    assert agent_core._before_tool_call(sid, "write_and_check_syntax", {
        "verilog_code": "module dut(input clk); always @(posedge clk) begin end endmodule",
        "top_module": "dut",
    }) is None


def test_material_revision_updates_architecture_and_invalidates_checkpoints(monkeypatch, tmp_path):
    sid, proposal = _proposal(monkeypatch, tmp_path)
    state = agent_core._get_state(sid)
    state["revisions"] = [{"revision": 1}]
    state["best_revision"] = {"revision": 1}
    revised = agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.revise",
        expected_revision=proposal["revision"], expected_digest=proposal["digest"],
        payload={"patch": {"clock_target_mhz": 40, "clock_period_ns": 25}})
    assert revised["status"] == "ready"
    assert agent_core._architecture_contract_for(state).clock_target_mhz == 40
    assert agent_core._architecture_contract_for(state).clock_period_ns == 25
    assert state["revisions"] == []
    assert state["best_revision"] is None


def test_new_design_prompt_clears_old_run_binding(monkeypatch, tmp_path):
    sid, proposal = _proposal(monkeypatch, tmp_path)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=proposal["revision"], expected_digest=proposal["digest"])
    state = agent_core._get_state(sid)
    assert state["contract_run_digest"]
    agent_core._begin_chat_run(sid, require_architecture=True)
    assert state["contract_run_digest"] is None
    assert state["contract_gate_pending"] is True



def test_changed_architecture_after_lock_creates_new_visible_gate(monkeypatch, tmp_path):
    sid, proposal = _proposal(monkeypatch, tmp_path)
    approved = agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=proposal["revision"], expected_digest=proposal["digest"])
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    state = agent_core._get_state(sid)
    state["current_request"] = "build dut with a deeper pipeline"

    changed = _architecture(top_module="dut_pipelined", clock_target_mhz=40,
                            clock_period_ns=25, pipeline_stages=2)
    raw = define_architecture(**changed)
    notice = agent_core._postprocess_tool_result(sid, "define_architecture", changed, raw)

    surface = agent_core.get_design_contract(sid)
    assert "ARCHITECTURE PROPOSAL READY" in notice
    assert surface["status"] == "ready"
    assert surface["revision"] > approved["revision"]
    assert surface["contract"]["spec"]["top_module"] == "dut_pipelined"
    assert state["contract_gate_pending"] is True
    assert state["contract_run_digest"] is None
    assert agent_core._before_tool_call(sid, "write_and_check_syntax", {
        "verilog_code": "module dut_pipelined(input clk); endmodule",
        "top_module": "dut_pipelined",
    }) is not None

