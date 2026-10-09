"""Focused tests for the restricted design-contract GenUI protocol."""

import json

import pytest
from fastapi.testclient import TestClient

import agent_core
from evidence import DesignSpec
from genui import (
    ContractProtocolError, apply_action, apply_component_ops, extract_contract,
)
from main import app


def _spec():
    return DesignSpec(
        design_id="demo", name="Demo", top_module="dut", target="sky130",
        metadata={"decisions": [{"id": "reset", "question": "Reset?",
                                  "options": ["sync", "async"]}]},
    )

def test_extraction_is_versioned_and_catalog_only():
    state = extract_contract(_spec(), project_id="proj-demo", session_id="proj-demo")
    assert state["contract_schema_version"] == "design-contract/v1"
    assert state["revision"] == 1
    assert {item["id"] for item in state["components"]} == {
        "contract.summary", "contract.interfaces", "contract.requirements",
        "contract.decisions", "contract.actions",
    }

def test_actions_require_revision_and_digest_and_never_accept_ui_code():
    state = extract_contract(_spec(), project_id="proj-demo", session_id="proj-demo")
    answered = apply_action(
        state["contract"], action="contract.answer_decision",
        project_id="proj-demo", session_id="proj-demo",
        expected_revision=1, expected_digest=state["digest"],
        payload={"decision_id": "reset", "answer": "sync"},
    )
    approved = apply_action(
        answered["contract"], action="contract.approve_and_build",
        project_id="proj-demo", session_id="proj-demo",
        expected_revision=answered["revision"], expected_digest=answered["digest"],
    )
    assert approved["status"] == "locked" and approved["revision"] == 3
    with pytest.raises(ContractProtocolError) as stale:
        apply_action(approved["contract"], action="contract.approve",
                     project_id="proj-demo", session_id="proj-demo",
                     expected_revision=1, expected_digest=state["digest"])
    assert stale.value.code == "stale_revision"
    with pytest.raises(ContractProtocolError) as forbidden:
        apply_component_ops(state["components"], [{"op": "upsert", "component": {
            "id": "contract.summary", "kind": "summary",
            "props": {"html": "<script>bad</script>"}}}])
    assert forbidden.value.code == "forbidden_ui"

def test_contract_action_over_ws_is_authoritative_and_no_eda(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    agent_core._project_repository = None
    sid = "proj-genui"
    agent_core._get_history(sid)
    initial = agent_core.extract_design_contract(sid, _spec())
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"project": sid}))
        assert ws.receive_json()["type"] == "history"
        joined = ws.receive_json()
        if joined["type"] == "project_state":
            joined = ws.receive_json()
        assert joined["type"] == "contract_state"
        ws.send_text(json.dumps({"type": "contract_action", "project": sid,
                                 "action": "contract.answer_decision",
                                 "expected_revision": initial["revision"],
                                 "expected_digest": initial["digest"],
                                 "payload": {"decision_id": "reset", "answer": "sync"}}))
        answered = ws.receive_json()
        assert answered["type"] == "contract_state"
        ws.send_text(json.dumps({"type": "contract_action", "project": sid,
                                 "action": "contract.approve_and_build",
                                 "expected_revision": answered["revision"],
                                 "expected_digest": answered["digest"]}))
        result = ws.receive_json()
        assert result["type"] == "contract_state"
        assert result["authoritative"] is True
        assert result["status"] == "locked"
        assert result["revision"] == 3
    saved = json.loads((tmp_path / f"{sid}.json").read_text())
    assert saved["design_contract"]["status"] == "locked"
    agent_core.reset_session(sid)


def test_stale_contract_action_returns_fresh_authoritative_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    agent_core._project_repository = None
    sid = "proj-genui-stale"
    agent_core._get_history(sid)
    initial = agent_core.extract_design_contract(sid, _spec())
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"project": sid}))
        while ws.receive_json()["type"] not in {"contract_state"}:
            pass
        ws.send_text(json.dumps({"type": "contract_action", "project": sid,
                                 "action": "contract.answer_decision",
                                 "expected_revision": initial["revision"],
                                 "expected_digest": initial["digest"],
                                 "payload": {"decision_id": "reset", "answer": "sync"}}))
        current = ws.receive_json()
        assert current["type"] == "contract_state"
        ws.send_text(json.dumps({"type": "contract_action", "project": sid,
                                 "action": "contract.approve_and_build",
                                 "expected_revision": initial["revision"],
                                 "expected_digest": initial["digest"]}))
        error = ws.receive_json()
        assert error["type"] == "contract_error"
        assert error["code"] == "stale_revision"
        assert error["authoritative_state"]["revision"] == current["revision"]
        assert error["authoritative_state"]["digest"] == current["digest"]
    agent_core.reset_session(sid)


def test_normal_architecture_approval_publishes_design_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    agent_core._project_repository = None
    sid = "proj-normal-contract"
    agent_core._get_history(sid)
    args = {
        "target_type": "asic", "top_module": "demo_top",
        "technology": "sky130", "clock_target_mhz": 50,
        "clock_period_ns": 20, "latency_cycles": 4,
        "throughput_per_cycle": 0.25, "pipeline_stages": 2,
        "multiplier_strategy": "inferred", "memory_strategy": "registers",
    }
    raw = agent_core.define_architecture(**args)
    result = agent_core._postprocess_tool_result(sid, "define_architecture", args, raw)
    surface = agent_core.get_design_contract(sid)
    assert "ARCHITECTURE PROPOSAL READY" in result
    assert surface["contract"]["spec"]["top_module"] == "demo_top"
    assert surface["contract"]["spec"]["timing_goals"]["clock_target_mhz"] == 50
    assert surface["revision"] == 1
    agent_core.reset_session(sid)


def test_compact_card_revision_patch_updates_typed_spec():
    state = extract_contract(_spec(), project_id="proj-demo", session_id="proj-demo")
    revised = apply_action(
        state["contract"], action="contract.revise",
        project_id="proj-demo", session_id="proj-demo",
        expected_revision=state["revision"], expected_digest=state["digest"],
        payload={"patch": {"clock_target_mhz": 75, "clock_period_ns": 13.333}},
    )
    goals = revised["contract"]["spec"]["timing_goals"]
    assert goals["clock_target_mhz"] == 75
    assert goals["clock_period_ns"] == 13.333


def test_contract_revision_diff_reports_pipeline_change():
    previous = {"spec": {"timing_goals": {"pipeline_stages": 1, "throughput_per_cycle": 0.1},
                           "metadata": {"architecture_contract": {"pipeline_stages": 1}}}}
    current = {"spec": {"timing_goals": {"pipeline_stages": 3, "throughput_per_cycle": 0.1},
                          "metadata": {"architecture_contract": {"pipeline_stages": 3}}}}
    diff = agent_core._contract_revision_diff(previous, current)
    assert {item["field"] for item in diff} == {"pipeline_stages"}
    assert diff[0]["previous"] == 1 and diff[0]["next"] == 3
