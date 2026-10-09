"""Offline S3 run-state/replay recovery checks (no provider or EDA calls)."""

import asyncio

import main
import agent_core


def test_authoritative_approval_is_not_replaced_with_completed(monkeypatch):
    session = main._SessionChat("proj-approval")
    session.run_id = "run-1"
    session.run_snapshot = {"run_id": "run-1", "seq": 2, "state": "running"}
    authoritative = {"run_id": "run-1", "seq": 3, "state": "awaiting approval",
                     "phase": "contract", "approval_pause": True,
                     "terminal_reason": "Awaiting contract approval."}
    monkeypatch.setattr(main, "_load_run_snapshot", lambda _sid: authoritative)
    adopted = session.sync_authoritative(main._load_run_snapshot(session.session_id))
    assert adopted["state"] == "awaiting approval"
    assert adopted["approval_pause"] is True


def test_delayed_snapshot_cannot_roll_back_session():
    session = main._SessionChat("proj-replay")
    session.run_id = "run-new"
    session.run_seq = 8
    session.run_snapshot = {"run_id": "run-new", "seq": 8, "state": "running"}
    old = {"run_id": "run-old", "seq": 4, "state": "completed"}
    assert session.sync_authoritative(old)["run_id"] == "run-new"
    assert session.run_snapshot["state"] == "running"


def test_terminal_snapshot_replays_once_without_duplicate_sequence(monkeypatch):
    session = main._SessionChat("proj-terminal")
    session.run_id = "run-terminal"
    session.run_snapshot = {"run_id": "run-terminal", "seq": 1, "state": "running"}
    authoritative = {"run_id": "run-terminal", "seq": 2, "state": "failed",
                     "phase": "synthesis", "terminal_reason": "tool failed"}
    monkeypatch.setattr(main, "_persist_run_snapshot", lambda _snapshot: None)
    session.sync_authoritative(authoritative)
    first_len = len(session.buffer)
    session.sync_authoritative(authoritative)
    # Same authoritative snapshot is idempotent in state; each live frame is
    # independently sequenced, while the durable controller sequence remains 2.
    assert session.run_snapshot["state"] == "failed"
    assert session.run_snapshot["seq"] == 2
    assert len(session.buffer) == first_len


def test_job_start_is_linked_once_to_durable_run(tmp_path, monkeypatch):
    sid = "proj-s3-job"
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_core, "_project_repository", None)
    agent_core._histories[sid] = [{"role": "system", "content": "test"}]
    state = agent_core._get_state(sid)
    state["controller_run_id"] = "run-job"
    agent_core._run_snapshot_for(sid, lifecycle="running")
    main._link_run_job(sid, "sim-123")
    main._link_run_job(sid, "sim-123")
    assert agent_core.get_project_state(sid)["run_state"]["jobs"] == ["sim-123"]
