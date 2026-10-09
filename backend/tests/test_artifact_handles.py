"""Regression tests for compact source-handle resolution."""
import asyncio
import json
from types import SimpleNamespace

import agent_core


class _Call:
    def __init__(self, name, args, ident):
        self.id = ident
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))


def _response(call=None, content=None):
    if call is None:
        msg = SimpleNamespace(
            content=content, tool_calls=None,
            model_dump=lambda exclude_none=True: {"role": "assistant", "content": content})
    else:
        msg = SimpleNamespace(
            content=None, tool_calls=[call],
            model_dump=lambda exclude_none=True: {
                "role": "assistant", "tool_calls": [{
                    "id": call.id, "type": "function",
                    "function": {"name": call.function.name,
                                 "arguments": call.function.arguments}}]})
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


def test_sim_then_compact_synthesis_handle_dispatches_exact_source(monkeypatch):
    sid = "artifact-handle-e2e"
    agent_core.reset_session(sid)
    rtl = "module matmul3(input clk);\n" + ("  wire [7:0] x;\n" * 120) + "endmodule"
    tb = "module tb; initial begin $display(\"PASS\"); end endmodule"
    marker = agent_core._artifact_handle_marker("rtl", rtl)
    responses = [
        _response(_Call("define_architecture", {
            "target_type": "asic", "top_module": "matmul3",
            "technology": "sky130", "clock_target_mhz": 50,
            "clock_period_ns": 20, "latency_cycles": 1,
            "throughput_per_cycle": 1, "pipeline_stages": 0,
            "multiplier_strategy": "inferred", "memory_strategy": "registers"
        }, "arch")),
        _response(_Call("run_simulation", {
            "verilog_code": rtl, "testbench_code": tb, "top_module": "matmul3"}, "sim")),
        _response(_Call("run_silicon_compiler", {
            "verilog_code": marker, "top_module": "matmul3", "clock_period_ns": 20}, "syn")),
        _response(content="done"),
    ]
    calls = []

    async def create(**kwargs):
        return responses.pop(0)

    fake_client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (fake_client, "fake"))
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "run_simulation",
                       lambda **kwargs: calls.append(("sim", kwargs)) or "SIMULATION PASSED")
    def synth(*, verilog_code, top_module, clock_period_ns=None, flow=None):
        calls.append(("syn", {"verilog_code": verilog_code, "top_module": top_module, "clock_period_ns": clock_period_ns, "flow": flow}))
        return "Synthesis successful!\nsetupslack: 1.0"
    monkeypatch.setitem(agent_core._TOOL_FUNCS, "run_silicon_compiler", synth)

    async def propose():
        return [event async for event in agent_core.chat("build", sid)]

    asyncio.run(propose())
    proposal = agent_core.get_design_contract(sid)
    agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.approve_and_build",
        expected_revision=proposal["revision"], expected_digest=proposal["digest"],
    )

    async def resume():
        return [event async for event in agent_core.chat(
            "build", sid, resume_contract=True)]

    asyncio.run(resume())
    assert calls[0][0] == "sim"
    assert calls[1] == ("syn", {"verilog_code": rtl, "top_module": "matmul3", "clock_period_ns": 20, "flow": "syn-sta"})
    assert agent_core._get_state(sid)["revision_by_hash"][agent_core._code_sha256(rtl)]["functional_passed"]
    agent_core.reset_session(sid)


def test_forged_or_cross_field_handle_is_blocked(monkeypatch):
    sid = "artifact-handle-forged"
    agent_core.reset_session(sid)
    rtl = "module m; endmodule"
    agent_core._begin_chat_run(sid)
    agent_core._postprocess_tool_result(
        sid, "run_simulation", {"verilog_code": rtl, "testbench_code": "tb", "top_module": "m"},
        "SIMULATION PASSED")
    history = [{"role": "assistant", "tool_calls": [{"id": "sim", "function": {
        "name": "run_simulation", "arguments": json.dumps({"verilog_code": rtl})}}]}]
    marker = agent_core._artifact_handle_marker("rtl", rtl)
    colon = marker.index(":")
    forged = marker[:colon + 1] + "0" * 16 + marker[colon + 17:]
    resolved, error = agent_core._resolve_artifact_handles_from_history(history, {
        "verilog_code": forged, "top_module": "m"})
    assert resolved["verilog_code"] == forged
    assert error and "not available" in error
    assert "Available rtl snapshot hashes" in error
    assert "CURRENT ARTIFACTS" in error
    mismatch, error = agent_core._resolve_artifact_handles_from_history(history, {
        "testbench_code": agent_core._artifact_handle_marker("rtl", rtl),
        "top_module": "m"})
    assert mismatch["testbench_code"].startswith("[artifact rtl:")
    assert error and "field mismatch" in error
    agent_core.reset_session(sid)


def test_passing_simulation_checkpoint_is_rebuilt_after_project_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    sid = "proj-restart-artifact"
    rtl = "module m; endmodule"
    tb = "module tb; endmodule"
    agent_core._histories[sid] = [{"role": "system", "content": agent_core.SYSTEM_INSTRUCTION}, {
        "role": "assistant", "tool_calls": [{"id": "sim", "type": "function", "function": {
            "name": "run_simulation", "arguments": json.dumps({
                "verilog_code": rtl, "testbench_code": tb, "top_module": "m"})}}]},
        {"role": "tool", "tool_call_id": "sim", "content": "SIMULATION PASSED"}]
    agent_core._project_names[sid] = "restart"
    agent_core._save_project(sid)
    agent_core._session_state.pop(sid, None)
    agent_core._histories.pop(sid, None)
    restored = agent_core._get_history(sid)
    assert restored
    state = agent_core._get_state(sid)
    assert state["revision_by_hash"][agent_core._code_sha256(rtl)]["functional_passed"] is True
    agent_core.reset_session(sid)



def test_project_state_rehydrates_full_rtl_when_latest_snapshot_is_marker(tmp_path, monkeypatch):
    """A stale JSON marker must not become the Code-tab contents on join."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    sid = "proj-state-marker"
    rtl = "module systolic_mm_4x4;\n" + ("  wire x;\n" * 160) + "endmodule"
    marker = agent_core._artifact_handle_marker("rtl", rtl)
    history = [
        {"role": "system", "content": agent_core.SYSTEM_INSTRUCTION},
        {"role": "assistant", "tool_calls": [{"id": "full", "type": "function", "function": {
            "name": "write_and_check_syntax", "arguments": json.dumps({
                "verilog_code": rtl, "top_module": "systolic_mm_4x4"})}}]},
        {"role": "assistant", "tool_calls": [{"id": "compact", "type": "function", "function": {
            "name": "run_silicon_compiler", "arguments": json.dumps({
                "verilog_code": marker, "top_module": "systolic_mm_4x4"})}}]},
    ]
    agent_core._histories[sid] = history
    agent_core._project_names[sid] = "marker state"
    agent_core._save_project(sid)
    # Simulate the stale pre-fix persisted field observed in the affected UI.
    data = agent_core._projects().load(sid)
    data["latest_verilog"] = {"top": "systolic_mm_4x4", "code": marker}
    agent_core._projects().save(sid, data)
    state = agent_core.get_project_state(sid)
    assert state["verilog"] == {"top": "systolic_mm_4x4", "code": rtl}
    assert not state["verilog"]["code"].startswith("[artifact ")
    agent_core.reset_session(sid)


def test_save_preserves_previous_full_dashboard_rtl_when_history_is_compacted(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    sid = "proj-state-fallback"
    rtl = "module systolic_mm_4x4; endmodule"
    marker = agent_core._artifact_handle_marker("rtl", rtl)
    agent_core._histories[sid] = [{"role": "system", "content": agent_core.SYSTEM_INSTRUCTION}]
    agent_core._project_names[sid] = "fallback"
    agent_core._save_project(sid)
    data = agent_core._projects().load(sid)
    data["latest_verilog"] = {"top": "systolic_mm_4x4", "code": rtl}
    data["history"] = [{"role": "system", "content": agent_core.SYSTEM_INSTRUCTION},
                       {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {
                           "name": "run_silicon_compiler", "arguments": json.dumps({
                               "verilog_code": marker, "top_module": "systolic_mm_4x4"})}}]}]
    agent_core._projects().save(sid, data)
    agent_core._histories[sid] = data["history"]
    agent_core._save_project(sid)
    saved = agent_core._projects().load(sid)
    assert saved["latest_verilog"] == {"top": "systolic_mm_4x4", "code": rtl}
    agent_core.reset_session(sid)
