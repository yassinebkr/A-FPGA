"""Phase 1 architecture-first admission tests; no model or EDA calls."""

import json

import agent_core
from architecture_contract import ArchitectureContract, define_architecture
from matmul_qualification import MATMUL_SPEC


def _payload(**changes):
    value = {
        "target_type": "asic",
        "top_module": "dut",
        "technology": "sky130",
        "clock_target_mhz": 50,
        "clock_period_ns": 20,
        "latency_cycles": 2,
        "throughput_per_cycle": 1,
        "pipeline_stages": 1,
        "multiplier_strategy": "inferred",
        "memory_strategy": "registers",
    }
    value.update(changes)
    return value


def test_contract_is_strict_and_hashed():
    result = json.loads(define_architecture(**_payload()))
    assert result["ok"] is True
    assert result["approved"] is True
    contract = ArchitectureContract.model_validate(result["contract"])
    assert result["contract_sha256"] == agent_core._architecture_contract_hash(contract)
    invalid = json.loads(define_architecture(**_payload(clock_period_ns=10)))
    assert invalid["ok"] is False


def test_autonomous_gate_requires_approval_and_rejects_contradiction():
    sid = "architecture-gate"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid, require_architecture=True)
    rtl = "module dut(input clk); always @(posedge clk) begin end endmodule"
    blocked = agent_core._before_tool_call(sid, "write_and_check_syntax", {
        "verilog_code": rtl, "top_module": "dut"})
    assert blocked and "CONTRACT BLOCKED" in blocked
    proposal = define_architecture(**_payload())
    agent_core._postprocess_tool_result(sid, "define_architecture", _payload(), proposal)
    surface = agent_core.get_design_contract(sid)
    assert agent_core._before_tool_call(sid, "write_and_check_syntax", {
        "verilog_code": rtl, "top_module": "dut"}) is not None
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"],
    )
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    assert agent_core._before_tool_call(sid, "write_and_check_syntax", {
        "verilog_code": rtl, "top_module": "dut"}) is None
    bad = agent_core._before_tool_call(sid, "run_silicon_compiler", {
        "verilog_code": rtl, "top_module": "dut", "clock_period_ns": 10})
    assert bad and "ARCHITECTURE CONTRADICTION" in bad
    agent_core.reset_session(sid)


def test_retry_implicitly_resumes_only_durable_locked_contract():
    sid = "implicit-resume"
    agent_core.reset_session(sid)
    payload = _payload()
    agent_core._begin_chat_run(sid, require_architecture=True)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload, define_architecture(**payload))
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"],
    )
    state = agent_core._get_state(sid)
    assert agent_core._implicit_contract_resume("retry", "Implement the dut accelerator", state)
    assert not agent_core._implicit_contract_resume("build a different accelerator", "build a different accelerator", state)
    state["pending_request"] = "waiting request"
    assert not agent_core._implicit_contract_resume("retry", "Implement the dut accelerator", state)
    agent_core.reset_session(sid)


def test_resume_contract_context_contains_approval_directive():
    sid = "resume-directive"
    agent_core.reset_session(sid)
    payload = _payload()
    agent_core._begin_chat_run(sid, require_architecture=True)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload, define_architecture(**payload))
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"],
    )
    state = agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    context = agent_core.build_model_context([{"role": "user", "content": "build it"}], state)
    dynamic = "\n".join(str(item.get("content") or "") for item in context)
    assert "APPROVAL ALREADY SUCCEEDED" in dynamic
    assert "Do not ask for readiness or approval" in dynamic
    assert "do not call define_architecture unless" in dynamic
    agent_core.reset_session(sid)


def test_locked_contract_repeated_architecture_with_new_rationale_continues(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    agent_core._project_repository = None
    sid = "locked-architecture-repeat"
    agent_core._get_history(sid)
    payload = _payload()
    agent_core._begin_chat_run(sid, require_architecture=True)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload, define_architecture(**payload))
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"],
    )
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    state = agent_core._get_state(sid)
    assert state["design_contract"]["status"] == "locked"
    repeated = dict(payload, rationale="same executable architecture, clarified rationale")
    result = agent_core._postprocess_tool_result(
        sid, "define_architecture", repeated, define_architecture(**repeated))
    assert "ARCHITECTURE ALREADY APPROVED" in result
    assert "approval_required: true" not in result
    assert state["contract_gate_pending"] is False
    assert state["design_contract"]["status"] == "locked"
    agent_core.delete_project(sid)


def test_locked_contract_internal_pipeline_change_continues_and_preserves_checkpoint():
    sid = "internal-architecture-optimization"
    agent_core.reset_session(sid)
    payload = _payload()
    agent_core._begin_chat_run(sid, require_architecture=True)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload,
                                        define_architecture(**payload))
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"],
    )
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    state = agent_core._get_state(sid)
    args = {"verilog_code": "module dut; endmodule", "testbench_code": "tb",
            "top_module": "dut"}
    agent_core._postprocess_tool_result(sid, "run_simulation", args,
                                        "SIMULATION PASSED")
    checkpoint = state["last_good_verilog"]

    optimized = dict(payload, pipeline_stages=2,
                     rationale="register the measured critical path")
    result = agent_core._postprocess_tool_result(
        sid, "define_architecture", optimized, define_architecture(**optimized))

    assert "ARCHITECTURE ALREADY APPROVED" in result
    assert '"approval_required": false' in result
    assert state["contract_gate_pending"] is False
    assert state["design_contract"]["status"] == "locked"
    assert state["design_contract"]["spec"]["timing_goals"]["pipeline_stages"] == 2
    assert state["last_good_verilog"] == checkpoint
    assert state["revisions"]
    agent_core.reset_session(sid)


def test_locked_contract_external_timing_change_still_requires_approval():
    sid = "external-architecture-change"
    agent_core.reset_session(sid)
    payload = _payload()
    agent_core._begin_chat_run(sid, require_architecture=True)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload,
                                        define_architecture(**payload))
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"],
    )
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)

    changed = dict(payload, latency_cycles=3)
    result = agent_core._postprocess_tool_result(
        sid, "define_architecture", changed, define_architecture(**changed))
    state = agent_core._get_state(sid)
    assert "ARCHITECTURE PROPOSAL READY" in result
    assert '"approval_required": true' in result
    assert state["contract_gate_pending"] is True
    assert state["design_contract"]["status"] == "ready"
    agent_core.reset_session(sid)


def test_locked_internal_optimization_does_not_reopen_gate_or_clear_baseline():
    sid = "internal-optimization"
    agent_core.reset_session(sid)
    payload = _payload()
    agent_core._begin_chat_run(sid, require_architecture=True)
    agent_core._postprocess_tool_result(sid, "define_architecture", payload, define_architecture(**payload))
    surface = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=surface["revision"], expected_digest=surface["digest"],
    )
    agent_core._begin_chat_run(sid, require_architecture=True, resume_contract=True)
    state = agent_core._get_state(sid)
    state["last_good_verilog"] = "module dut; endmodule"
    state["revisions"] = [{"revision": 1, "functional_passed": True}]
    optimized = _payload(pipeline_stages=2, multiplier_strategy="dsp",
                         memory_strategy="distributed_ram", rationale="shorter datapath")
    result = agent_core._postprocess_tool_result(
        sid, "define_architecture", optimized, define_architecture(**optimized))
    assert "ARCHITECTURE ALREADY APPROVED" in result
    assert "approval_required: true" not in result
    assert "internal optimization accepted" in result
    assert state["contract_gate_pending"] is False
    assert state["last_good_verilog"] == "module dut; endmodule"
    assert state["revisions"]
    assert state["architecture_contract"]["pipeline_stages"] == 2
    assert state["design_contract"]["status"] == "locked"
    assert state["design_contract"]["spec"]["timing_goals"]["pipeline_stages"] == 2
    agent_core.reset_session(sid)


def test_frozen_matmul3_admission_stays_sticky_on_generic_continuation():
    sid = "sticky-frozen-matmul3"
    agent_core.reset_session(sid)
    state = agent_core._begin_chat_run(sid, require_architecture=True)
    state["current_request"] = "Implement the frozen matmul3 qualification benchmark exactly."
    exact = _payload(
        top_module=MATMUL_SPEC.top_module,
        technology=MATMUL_SPEC.technology,
        clock_target_mhz=MATMUL_SPEC.target_fmax_mhz,
        clock_period_ns=MATMUL_SPEC.target_period_ns,
        latency_cycles=MATMUL_SPEC.latency_cycles,
        throughput_per_cycle=1 / MATMUL_SPEC.initiation_interval_cycles,
        pipeline_stages=3,
    )
    assert "ARCHITECTURE PROPOSAL READY" in agent_core._postprocess_tool_result(
        sid, "define_architecture", exact, define_architecture(**exact))
    assert state["benchmark_contract_id"] == "matmul3"
    state["current_request"] = "continue the build"
    wrong = dict(exact, throughput_per_cycle=1.6)
    result = agent_core._postprocess_tool_result(
        sid, "define_architecture", wrong, define_architecture(**wrong))
    assert "frozen matmul3 contract mismatch" in result
    agent_core.reset_session(sid)


def test_architecture_change_invalidates_checkpoints():
    sid = "architecture-change"
    agent_core.reset_session(sid)
    agent_core._begin_chat_run(sid, require_architecture=True)
    first = _payload()
    agent_core._postprocess_tool_result(sid, "define_architecture", first, define_architecture(**first))
    args = {"verilog_code": "module dut(input clk); always @(posedge clk) begin end endmodule",
            "testbench_code": "tb", "top_module": "dut"}
    agent_core._postprocess_tool_result(sid, "run_simulation", args, "SIMULATION PASSED")
    assert agent_core._get_state(sid)["revisions"]
    second = _payload(clock_target_mhz=40, clock_period_ns=25)
    agent_core._postprocess_tool_result(sid, "define_architecture", second, define_architecture(**second))
    state = agent_core._get_state(sid)
    assert state["revisions"] == []
    assert state["last_good_verilog"] is None
    agent_core.reset_session(sid)


def test_contract_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    agent_core._project_repository = None
    project = agent_core.create_project("Architecture persistence")
    sid = project["id"]
    payload = _payload()
    agent_core._postprocess_tool_result(sid, "define_architecture", payload, define_architecture(**payload))
    agent_core._save_project(sid)
    expected = agent_core._get_state(sid)["architecture_contract_hash"]
    agent_core._histories.pop(sid, None)
    agent_core._session_state.pop(sid, None)
    agent_core._get_history(sid)
    assert agent_core._get_state(sid)["architecture_contract_hash"] == expected
    agent_core.delete_project(sid)


def test_frozen_matmul3_context_injects_canonical_spec_only_for_explicit_benchmark():
    request = "Implement the frozen matmul3 qualification benchmark exactly."
    history = [{"role": "user", "content": request}]
    context = agent_core.build_model_context(
        history, {"current_request": request}
    )
    dynamic = "\n".join(str(item.get("content") or "") for item in context)
    assert "FROZEN BENCHMARK CONTRACT — matmul3 (sole source of truth)" in dynamic
    assert MATMUL_SPEC.canonical()["interfaces"]
    assert '"initiation_interval_cycles": 10' in dynamic
    assert "throughput_per_cycle = 1 / 10 = 0.1" in dynamic
    assert '"top_module": "systolic_mm_4x4"' in dynamic

    generic = "Design a matrix multiplication accelerator."
    generic_context = agent_core.build_model_context(
        [{"role": "user", "content": generic}],
        {"current_request": generic},
    )
    generic_text = "\n".join(str(item.get("content") or "") for item in generic_context)
    assert "FROZEN BENCHMARK CONTRACT" not in generic_text


def test_frozen_matmul3_admission_rejects_incompatible_proposal_before_state_update():
    sid = "frozen-matmul3-admission"
    agent_core.reset_session(sid)
    state = agent_core._begin_chat_run(sid, require_architecture=True)
    state["current_request"] = (
        "Implement the frozen matmul3 qualification benchmark using the exact contract."
    )
    wrong = _payload(
        top_module="wrong_top", latency_cycles=4, throughput_per_cycle=1
    )
    result = agent_core._postprocess_tool_result(
        sid, "define_architecture", wrong, define_architecture(**wrong)
    )
    assert "ARCHITECTURE REJECTED" in result
    assert "frozen matmul3 contract mismatch" in result
    assert agent_core._get_state(sid)["architecture_contract"] is None

    exact = _payload(
        top_module=MATMUL_SPEC.top_module,
        technology=MATMUL_SPEC.technology,
        clock_target_mhz=MATMUL_SPEC.target_fmax_mhz,
        clock_period_ns=MATMUL_SPEC.target_period_ns,
        latency_cycles=MATMUL_SPEC.latency_cycles,
        throughput_per_cycle=1 / MATMUL_SPEC.initiation_interval_cycles,
        pipeline_stages=3,
    )
    accepted = agent_core._postprocess_tool_result(
        sid, "define_architecture", exact, define_architecture(**exact)
    )
    assert "ARCHITECTURE PROPOSAL READY" in accepted
    assert agent_core._get_state(sid)["architecture_contract"]["top_module"] == MATMUL_SPEC.top_module
    assert agent_core._get_state(sid)["architecture_contract"]["throughput_per_cycle"] == 0.1
    agent_core.reset_session(sid)


def test_retry_inherits_latest_substantive_matmul3_request():
    original = "Implement the frozen matmul3 qualification benchmark exactly."
    history = [
        {"role": "user", "content": original},
        {"role": "assistant", "content": "The prior run stopped."},
    ]
    assert agent_core._effective_request("retry", history) == original
    assert agent_core._is_frozen_matmul3_request(
        agent_core._effective_request("continue", history))
    assert agent_core._effective_request("design a counter", history) == "design a counter"


def test_frozen_matmul3_sticky_binding_rejects_bad_continuation_throughput():
    sid = "frozen-matmul3-sticky"
    agent_core.reset_session(sid)
    state = agent_core._begin_chat_run(sid, require_architecture=True)
    state["benchmark_contract_id"] = "matmul3"
    state["current_request"] = "go ahead"
    wrong = _payload(
        top_module=MATMUL_SPEC.top_module,
        technology=MATMUL_SPEC.technology,
        clock_target_mhz=MATMUL_SPEC.target_fmax_mhz,
        clock_period_ns=MATMUL_SPEC.target_period_ns,
        latency_cycles=MATMUL_SPEC.latency_cycles,
        throughput_per_cycle=1.6,
        pipeline_stages=4,
    )
    result = agent_core._postprocess_tool_result(
        sid, "define_architecture", wrong, define_architecture(**wrong))
    assert "ARCHITECTURE REJECTED" in result
    assert "throughput_per_cycle" in result
    assert state["architecture_contract"] is None
    agent_core.reset_session(sid)
