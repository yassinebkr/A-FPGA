import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import eda_tools
import verification_api
from verification_api import router
from verification_pipeline import VerificationGate, GateStatus, VerificationGateError


class _FakeResult:
    job_id = "ver-123"

    def as_dict(self):
        return {
            "manifest": {"status": "completed"},
            "report": {"overall_status": "passed"},
            "requirement_matrix": [{"requirement_id": "r", "status": "passed"}],
            "gate": {"allowed": True, "status": "passed"},
            "job_id": self.job_id,
        }


def test_structured_verification_api_returns_gate_and_matrix(monkeypatch):
    async def fake_job(*args, **kwargs):
        return _FakeResult()

    class FakePipeline:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(verification_api, "VerificationPipeline", FakePipeline)
    monkeypatch.setattr(verification_api, "run_verification_job", fake_job)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/verification/runs", json={
            "verilog_code": "module dut; endmodule",
            "testbench_code": "module tb; endmodule",
            "top_module": "dut",
        })
    assert response.status_code == 201
    body = response.json()
    assert body["gate"]["allowed"] is True
    assert body["requirement_matrix"][0]["status"] == "passed"


def test_verification_api_rejects_spec_top_mismatch():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/verification/runs", json={
            "verilog_code": "module dut; endmodule",
            "top_module": "dut",
            "design_spec": {
                "design_id": "other", "name": "Other", "top_module": "other",
            },
        })
    assert response.status_code == 422


def test_verification_api_accepts_typed_constraint_shorthand(monkeypatch):
    async def fake_job(*args, **kwargs):
        return _FakeResult()

    class FakePipeline:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(verification_api, "VerificationPipeline", FakePipeline)
    monkeypatch.setattr(verification_api, "run_verification_job", fake_job)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/verification/runs", json={
            "verilog_code": "module dut; endmodule",
            "top_module": "dut",
            "constraints": {"clocks": [{"name": "clk", "port": "clk",
                                          "period_ns": 10}]},
        })
    assert response.status_code == 201


def test_agent_tool_returns_structured_payload_and_job_marker(monkeypatch):
    class FakePipeline:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def fake_job(*args, **kwargs):
        return _FakeResult()

    monkeypatch.setattr("verification_pipeline.VerificationPipeline", FakePipeline)
    monkeypatch.setattr("verification_pipeline.run_verification_job", fake_job)
    result = asyncio.run(eda_tools.run_verification(
        "module dut; endmodule", "module tb; endmodule", "dut"))
    payload, marker = result.rsplit("\njob_id: ", 1)
    assert json.loads(payload)["gate"]["allowed"] is True
    assert marker == "ver-123"


def test_eda_synthesis_guard_is_explicit_and_reusable():
    denied = {"gate": {"allowed": False, "status": "inconclusive",
                        "blocking_stages": ["structural_coverage"],
                        "blocking_requirements": [],
                        "reason": "coverage unavailable"}}
    try:
        eda_tools.require_synthesis_allowed(denied)
    except VerificationGateError as exc:
        assert exc.gate.status is GateStatus.INCONCLUSIVE
        assert "structural_coverage" in exc.gate.blocking_stages
    else:
        raise AssertionError("inconclusive verification must block synthesis")
