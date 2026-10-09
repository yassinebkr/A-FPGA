from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_core
import hardware_project
from evidence import EvidenceRepository
from main import app


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    repo = EvidenceRepository(tmp_path / "evidence")
    monkeypatch.setattr(hardware_project, "repository", repo)
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    agent_core._histories.clear()
    agent_core._project_names.clear()
    yield repo
    agent_core._histories.clear()
    agent_core._project_names.clear()


def payload():
    return {
        "top_module": "soc_top",
        "files": [
            {"path": "rtl/pkg.sv", "role": "package",
             "content": "package core_pkg; parameter W=32; endpackage\n"},
            {"path": "rtl/alu.sv", "role": "rtl",
             "content": "module alu(input logic a, output logic y); assign y=a; endmodule\n"},
            {"path": "rtl/soc_top.sv", "role": "rtl",
             "content": "module soc_top(input logic a, output logic y); alu u(.a(a),.y(y)); endmodule\n"},
            {"path": "tb/soc_tb.sv", "role": "testbench",
             "content": "module soc_tb; endmodule\n"},
            {"path": "constraints/top.sdc", "role": "constraint",
             "content": "create_clock -period 20 [get_ports clk]\n"},
        ],
        "compile_order": ["rtl/pkg.sv", "rtl/alu.sv", "rtl/soc_top.sv"],
        "include_dirs": ["rtl"],
        "defines": {"SYNTHESIS": None},
        "testbenches": ["tb/soc_tb.sv"],
        "constraints": ["constraints/top.sdc"],
        "build_targets": {"sky130": {"target": "skywater130_demo"}},
    }


def test_multifile_manifest_survives_reload_with_identical_hashes(isolated):
    project = agent_core.create_project("multi")
    update = hardware_project.HardwareProjectUpdate.model_validate(payload())
    first = agent_core.replace_hardware_project(project["id"], update)
    agent_core._histories.pop(project["id"], None)
    agent_core._project_names.pop(project["id"], None)
    restored = agent_core.get_hardware_project(project["id"])
    assert restored["digest"] == first["digest"]
    assert [item["sha256"] for item in restored["files"]] == [
        item["sha256"] for item in first["files"]]
    assert [item["content"] for item in restored["files"]] == [
        item["content"] for item in first["files"]]


def test_unsafe_and_incomplete_updates_fail_before_replacing_manifest(isolated):
    project = agent_core.create_project("atomic")
    first = agent_core.replace_hardware_project(
        project["id"], hardware_project.HardwareProjectUpdate.model_validate(payload()))
    bad = payload()
    bad["files"][0]["path"] = "../escape.sv"
    with pytest.raises(ValueError):
        hardware_project.HardwareProjectUpdate.model_validate(bad)
    incomplete = payload()
    incomplete["compile_order"] = ["rtl/soc_top.sv"]
    with pytest.raises(ValueError):
        hardware_project.HardwareProjectUpdate.model_validate(incomplete)
    assert agent_core.get_hardware_project(project["id"])["digest"] == first["digest"]


def test_legacy_single_file_project_remains_unchanged(isolated):
    project = agent_core.create_project("legacy")
    data = agent_core._projects().load(project["id"])
    data["latest_verilog"] = {"top": "legacy", "code": "module legacy; endmodule\n"}
    agent_core._projects().save(project["id"], data)
    state = agent_core.get_project_state(project["id"])
    assert state["verilog"]["code"] == "module legacy; endmodule\n"
    assert "hardware_project" not in state


def test_project_state_and_rest_api_expose_selected_files(isolated):
    project = agent_core.create_project("api")
    client = TestClient(app)
    response = client.put(
        f"/api/projects/{project['id']}/hardware-project", json=payload())
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == hardware_project.SCHEMA
    assert len(body["files"]) == 5
    assert agent_core.get_project_state(project["id"])["hardware_project"]["digest"] == body["digest"]
    state = agent_core.get_project_state(project["id"])
    assert "metrics" not in state
    assert state["measurement_status"]["reason"] == "source_set_changed"
    fetched = client.get(f"/api/projects/{project['id']}/hardware-project")
    assert fetched.status_code == 200
    assert fetched.json()["digest"] == body["digest"]
