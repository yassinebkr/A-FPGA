"""S8 qualification scenarios using synthetic provider/EDA evidence only."""

import asyncio
from types import SimpleNamespace

import agent_core
import main
import pytest


def _chunk(*, content=None, usage=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, reasoning_content=None,
                               reasoning=None, tool_calls=None), finish_reason=None)],
                           usage=usage)


class _RecordedStream:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.chunks:
            raise StopAsyncIteration
        item = self.chunks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _sim(sid, code, top="counter8"):
    return agent_core._postprocess_tool_result(
        sid, "run_simulation", {"verilog_code": code, "testbench_code": "tb-v1",
                                 "top_module": top},
        "SIMULATION PASSED (recorded fixture)")


def _screen(sid, code, slack, top="counter8", job="screen-fixture"):
    return agent_core._postprocess_tool_result(
        sid, "run_silicon_compiler", {"verilog_code": code, "top_module": top,
                                       "target": "skywater130_demo", "clock_period_ns": 20},
        "Synthesis successful (recorded fixture)\n"
        f"setupslack: {slack}\nfmax_mhz: {1000 / (20 - slack):.3f}\njob_id: {job}")


@pytest.fixture
def clean_session():
    ids = []
    yield ids
    for sid in ids:
        agent_core.reset_session(sid)


def test_unknown_timing_path_recovers_once_then_terminates(clean_session):
    sid = "s8-unknown-recovery"
    clean_session.append(sid)
    code = "module counter8; wire opaque_path; endmodule"
    _sim(sid, code)
    state = agent_core._get_state(sid)
    state["retry_constraint"] = {"reason": "timing"}
    state["timing_diagnosis"] = {"critical_path_classification": {"category": "unknown"}, "paths": [{"startpoint": "u0/Q", "endpoint": "u1/D", "slack_ns": -1.0}]}
    blocked = agent_core._before_tool_call(
        sid, "run_silicon_compiler", {"verilog_code": code, "top_module": "counter8", "flow": "syn-sta"})
    assert blocked and "reason_code=diagnosis_inconclusive" in blocked
    assert state["synthesis_invocations"] == 0


def test_improvement_then_regression_preserves_best_for_non_matmul(clean_session):
    sid = "s8-counter8-lineage"
    clean_session.append(sid)
    baseline = "module counter8(input clk); always @(posedge clk); endmodule"
    improved = "module counter8(input clk); reg [7:0] q; always @(posedge clk) q <= q; endmodule"
    regression = "module counter8(input clk); reg [31:0] q; always @(posedge clk) q <= q + 32'd1; endmodule"
    _sim(sid, baseline)
    assert "REVISION ACCEPTED" in _screen(sid, baseline, 0.1, job="counter-base")
    _sim(sid, improved)
    assert "REVISION ACCEPTED" in _screen(sid, improved, 0.8, job="counter-best")
    _sim(sid, regression)
    assert "REVISION REJECTED" in _screen(sid, regression, -0.2, job="counter-worse")
    state = agent_core._get_state(sid)
    assert state["active_revision"]["code_sha256"] == agent_core._code_sha256(improved)
    assert state["latest_attempted_revision"]["code_sha256"] == agent_core._code_sha256(regression)


def test_stale_approval_is_rejected_with_authoritative_contract(clean_session):
    from evidence import DesignSpec
    sid = "s8-stale-approval"
    clean_session.append(sid)
    agent_core._get_history(sid)
    state = agent_core.extract_design_contract(
        sid, DesignSpec(design_id="counter8", name="Counter", top_module="counter8", target="sky130"))
    revised = agent_core.apply_design_contract_action(
        sid, project_id=sid, action="contract.revise", expected_revision=state["revision"],
        expected_digest=state["digest"], payload={"patch": {"clock_target_mhz": 40, "clock_period_ns": 25}})
    with pytest.raises(Exception) as error:
        agent_core.apply_design_contract_action(
            sid, project_id=sid, action="contract.approve_and_build",
            expected_revision=state["revision"], expected_digest=state["digest"])
    assert getattr(error.value, "code", None) == "stale_revision"
    assert revised["revision"] > state["revision"]


def test_mocked_provider_stream_records_usage_without_network(clean_session, monkeypatch):
    sid = "s8-provider-stream"
    clean_session.append(sid)
    stream = _RecordedStream([_chunk(content="counter8 ready"), _chunk(
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5))])
    async def create(**_):
        return stream

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_core, "_client_for", lambda _: (client, "offline-fixture"))

    async def run():
        return [event async for event in agent_core.chat("inspect counter8", sid)]

    events = asyncio.run(run())
    assert any(e["type"] == "text_delta" and e["content"] == "counter8 ready" for e in events)
    assert any(e["type"] == "generation_usage" and e["total_tokens"] == 5 for e in events)


def test_cancellation_reconnect_and_restart_keep_durable_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    sid = "proj-s8-recovery"
    agent_core._get_history(sid)
    state = agent_core._get_state(sid)
    state["controller_run_id"] = "run-s8-recovery"
    agent_core._run_snapshot_for(sid, lifecycle="cancelled", terminal_reason="Run cancelled by user.")
    agent_core._save_project(sid)
    session = main._SessionChat(sid)
    session.load_snapshot()
    assert session.run_snapshot["state"] == "cancelled"
    assert session.run_snapshot["terminal_reason"] == "Run cancelled by user."
    session.sync_authoritative({"run_id": "run-s8-recovery", "seq": 0, "lifecycle": "running"})
    assert session.run_snapshot["state"] == "cancelled"
    agent_core.reset_session(sid)


def test_budget_termination_is_visible_and_bounded(clean_session, monkeypatch):
    sid = "s8-budget"
    clean_session.append(sid)
    monkeypatch.setattr(agent_core, "TOTAL_REPAIR_CAP", 1)
    args = {"verilog_code": "module counter8; endmodule", "testbench_code": "tb", "top_module": "counter8"}
    first = agent_core._postprocess_tool_result(sid, "run_simulation", args, "SIMULATION FAILED (fixture)")
    second = agent_core._postprocess_tool_result(sid, "run_simulation", args, "SIMULATION FAILED (fixture)")
    assert "BUDGET EXHAUSTED" in second
    assert "tokens_used (this session):" in second or "tokens_remaining:" in second
    assert first != second
