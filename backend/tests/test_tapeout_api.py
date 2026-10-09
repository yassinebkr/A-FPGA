import hashlib
import json

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import tapeout_api
from tapeout_repro import (
    HardeningStage, InputHash, ReleaseGate, TapeoutEvidenceRepository,
    TinyTapeoutRun, ToolIdentity,
)


def test_tapeout_run_route_returns_job_and_hardening_payload(monkeypatch):
    async def fake_job(*args, **kwargs):
        return json.dumps({"status": "inconclusive", "ok": False}) + "\njob_id: tap-123"

    monkeypatch.setattr(tapeout_api, "run_tiny_tapeout_hardening_job", fake_job)
    app = FastAPI()
    app.include_router(tapeout_api.router)
    with TestClient(app) as client:
        response = client.post("/api/tapeout/runs", json={
            "wrapper_verilog": "module tt_um_demo; endmodule",
            "top_module": "demo",
            "clock_hz": 50_000_000,
        })
    assert response.status_code == 201
    assert response.json()["job_id"] == "tap-123"
    assert response.json()["status"] == "inconclusive"


def test_release_gate_route_fails_closed_for_incomplete_record():
    app = FastAPI()
    app.include_router(tapeout_api.router)
    run = TinyTapeoutRun(run_id="r1", design_id="d", top_module="tt_um_d")
    with TestClient(app) as client:
        response = client.post("/api/tapeout/release-gate", json={"run": run.model_dump(mode="json")})
    assert response.status_code == 200
    body = response.json()
    assert body["eligible"] is False
    assert body["assurance"] == "local_dry_run"
    assert body["reasons"]


def test_agent_schema_and_dispatch_include_tapeout_tool():
    import agent_core

    names = {item["function"]["name"] for item in agent_core.TOOLS}
    assert "run_tiny_tapeout_hardening" in names
    assert agent_core._TOOL_FUNCS["run_tiny_tapeout_hardening"] is not None



def _saved_evidence(tmp_path):
    repo = TapeoutEvidenceRepository(tmp_path / "evidence")
    run = TinyTapeoutRun(run_id="tt-safe-1", design_id="demo", top_module="tt_um_demo")
    gate = ReleaseGate(eligible=False, reasons=["incomplete hardening"])
    digest, path = repo.save(run, gate)
    return repo, run, gate, digest, path


def _api_client(monkeypatch, repo):
    monkeypatch.setattr(tapeout_api, "evidence_repository", repo)
    app = FastAPI()
    app.include_router(tapeout_api.router)
    return TestClient(app)


def test_tapeout_evidence_get_and_list_aliases_verify_digest(tmp_path, monkeypatch):
    repo, run, gate, digest, path = _saved_evidence(tmp_path)
    with _api_client(monkeypatch, repo) as client:
        expected = [{
            "id": run.run_id, "digest": digest,
            "run": run.model_dump(mode="json"),
            "release_gate": gate.model_dump(mode="json"),
        }]
        for endpoint in ("/api/tapeout/runs", "/api/tapeout/evidence"):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert response.json() == expected
        for endpoint in (f"/api/tapeout/runs/{run.run_id}",
                         f"/api/tapeout/evidence/{run.run_id}"):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert response.json()["digest"] == digest
            assert response.json()["run"]["run_id"] == run.run_id
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("run_id", ["missing", "../escape", "..", "bad/id"])
def test_tapeout_evidence_get_returns_404_for_missing_or_unsafe_id(tmp_path, monkeypatch, run_id):
    repo, *_ = _saved_evidence(tmp_path)
    with _api_client(monkeypatch, repo) as client:
        response = client.get(f"/api/tapeout/runs/{run_id}")
    assert response.status_code == 404


def test_tapeout_evidence_list_ignores_corrupt_and_symlink_records(tmp_path, monkeypatch):
    repo, run, gate, *_ = _saved_evidence(tmp_path)
    (repo.runs / "corrupt.json").write_text("not json", encoding="utf-8")
    invalid = {"run": {"run_id": "invalid"}, "release_gate": {"eligible": False}}
    (repo.runs / "invalid.json").write_text(json.dumps(invalid), encoding="utf-8")
    (repo.runs / "link.json").symlink_to(repo.runs / f"{run.run_id}.json")
    with _api_client(monkeypatch, repo) as client:
        response = client.get("/api/tapeout/runs")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [run.run_id]
        assert client.get("/api/tapeout/runs/corrupt").status_code == 404
        assert client.get("/api/tapeout/runs/invalid").status_code == 404
        assert client.get("/api/tapeout/runs/link").status_code == 404
