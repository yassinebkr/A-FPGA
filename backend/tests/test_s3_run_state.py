"""Focused S3 durable run-state regressions."""

import asyncio

import agent_core


def _project(tmp_path, monkeypatch, sid="proj-s3"):
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    agent_core._histories[sid] = [{"role": "system", "content": "test"}]
    agent_core._project_names[sid] = "S3"
    agent_core._session_state.pop(sid, None)
    return sid


def _persist_running(sid):
    state = agent_core._get_state(sid)
    state["controller_run_id"] = "run-s3"
    agent_core._run_snapshot_for(sid, lifecycle="running")
    agent_core._save_project(sid)


def _reload(sid):
    agent_core._histories.pop(sid, None)
    agent_core._project_timelines.pop(sid, None)
    agent_core._session_state.pop(sid, None)
    agent_core._load_project(sid)
    return agent_core._get_state(sid)["run_snapshot"]


def test_running_becomes_interrupted_on_reload(tmp_path, monkeypatch):
    sid = _project(tmp_path, monkeypatch)
    _persist_running(sid)
    snapshot = _reload(sid)
    assert snapshot["lifecycle"] == "interrupted"
    assert "restarted" in snapshot["terminal_reason"]


def test_awaiting_approval_survives_reload(tmp_path, monkeypatch):
    sid = _project(tmp_path, monkeypatch, "proj-s3-approval")
    state = agent_core._get_state(sid)
    state["controller_run_id"] = "run-approval"
    state["contract_gate_pending"] = True
    agent_core._run_snapshot_for(sid, lifecycle="awaiting approval")
    agent_core._save_project(sid)
    assert _reload(sid)["lifecycle"] == "awaiting approval"


def test_terminal_states_survive_reload(tmp_path, monkeypatch):
    for lifecycle in ("completed", "failed", "cancelled", "inconclusive"):
        sid = _project(tmp_path, monkeypatch, "proj-s3-" + lifecycle)
        state = agent_core._get_state(sid)
        state["controller_run_id"] = "run-" + lifecycle
        agent_core._run_snapshot_for(sid, lifecycle=lifecycle,
                                     terminal_reason="fixture reason")
        agent_core._save_project(sid)
        restored = _reload(sid)
        assert restored["lifecycle"] == lifecycle
        assert restored["terminal_reason"] == "fixture reason"


def test_duplicate_timeline_event_does_not_increment_run_sequence(tmp_path, monkeypatch):
    sid = _project(tmp_path, monkeypatch, "proj-s3-seq")
    state = agent_core._get_state(sid)
    state["controller_run_id"] = "run-seq"
    event = {"type": "decision", "content": {
        "event_id": "same", "phase": "run", "action": "done", "status": "completed"}}
    assert agent_core.record_public_timeline_event(sid, event)
    first = agent_core.get_project_state(sid)["run_state"]["seq"]
    assert agent_core.record_public_timeline_event(sid, event)
    second = agent_core.get_project_state(sid)["run_state"]["seq"]
    assert first == second


def test_error_frame_persists_failed_terminal_reason(tmp_path, monkeypatch):
    sid = _project(tmp_path, monkeypatch, "proj-s3-error")

    async def stopped(*_args, **_kwargs):
        yield {"type": "error", "content": "provider timed out"}

    monkeypatch.setattr(agent_core, "_chat_inner", stopped)
    async def collect():
        return [event async for event in agent_core.chat("go", sid)]

    events = asyncio.run(collect())
    assert events[-1]["type"] == "error"
    snapshot = agent_core.get_project_state(sid)["run_state"]
    assert snapshot["lifecycle"] == "failed"
    assert snapshot["terminal_reason"] == "provider timed out"
