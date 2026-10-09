import asyncio
import json
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient

import agent_core
from main import app

client = TestClient(app)


def recv(ws):
    """Next protocol frame, skipping durable ``run_state`` snapshots.

    Chat frames also carry a wall-clock ``ts`` stamp for the console's
    discreet per-row timestamps; equality assertions in this suite target
    the protocol payload, so the stamp is dropped here.

    Part 1 of the jobs-workbench plan added authoritative run_state frames at
    every model-round/tool boundary; they are covered by the dedicated run
    state suite (tests/test_s3_run_state.py). These protocol tests assert the
    chat/tool/job frame contract, so they deliberately skip the snapshots.
    """
    while True:
        frame = ws.receive_json()
        if frame.get("type") != "run_state":
            frame.pop("ts", None)
            return frame


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "api_key_set" in body


def test_websocket_text_then_end():
    async def fake_chat(message, session_id="default"):
        yield {"type": "text", "content": "Synthesis complete"}

    with patch("main.agent_core.chat", side_effect=fake_chat):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("Hello Agent")

            resp1 = recv(websocket)
            assert resp1["type"] == "text"
            assert resp1["content"] == "Synthesis complete"

            resp2 = recv(websocket)
            assert resp2["type"] == "end"


def test_websocket_tool_frames_passthrough():
    async def fake_chat(message, session_id="default"):
        yield {"type": "tool_call", "content": json.dumps({"name": "run_silicon_compiler", "args": {}})}
        yield {"type": "tool_result", "content": "Synthesis successful!"}
        yield {"type": "text", "content": "Done"}

    with patch("main.agent_core.chat", side_effect=fake_chat):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("Build a MAC")

            frames = [recv(websocket) for _ in range(4)]
            types = [frame["type"] for frame in frames]
            assert types == ["tool_call", "tool_result", "text", "end"]


def test_websocket_error_frame_on_exception():
    async def failing_chat(message, session_id="default"):
        raise RuntimeError("boom")
        yield  # make it an async generator

    with patch("main.agent_core.chat", side_effect=failing_chat):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text("Hi")

            resp1 = recv(websocket)
            assert resp1["type"] == "error"
            assert "boom" in resp1["content"]

            resp2 = recv(websocket)
            assert resp2["type"] == "end"



def test_disconnect_lets_chat_task_run(caplog):
    """Closing the WebSocket mid-chat must not cancel the agent run:
    the finally block must skip the in-flight chat task (logged as
    'letting chat task finish in background'). Note: TestClient itself
    reaps stray tasks on disconnect, so survival past that point can only
    be verified against a real server (done manually with uvicorn)."""
    import asyncio
    import logging

    async def slow_chat(message, session_id="default"):
        await asyncio.sleep(30)
        yield {"type": "text", "content": "late answer"}

    with patch("main.agent_core.chat", side_effect=slow_chat):
        with caplog.at_level(logging.INFO, logger="server"):
            with client.websocket_connect("/ws") as websocket:
                websocket.send_text("long running prompt")
    assert any("letting chat task finish in background" in r.message
               for r in caplog.records), "chat task was not spared on disconnect"


def _cleanup_project(session_id):
    agent_core._histories.pop(session_id, None)
    agent_core._project_names.pop(session_id, None)
    agent_core._project_models.pop(session_id, None)


def test_concurrent_chat_same_session_rejected(tmp_path, monkeypatch):
    """A second chat for the SAME project session (e.g. resent after a page
    refresh) is rejected immediately instead of running a second agent loop."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    started = threading.Event()
    release = threading.Event()

    async def slow_chat(message, session_id="default"):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield {"type": "text", "content": "done"}

    with patch("main.agent_core.chat", side_effect=slow_chat):
        with client.websocket_connect("/ws") as ws1:
            with client.websocket_connect("/ws") as ws2:
                for ws in (ws1, ws2):
                    ws.send_text(json.dumps({"project": "proj-locktest"}))
                    assert ws.receive_json()["type"] == "history"
                ws1.send_text("first")
                assert started.wait(timeout=5), "first chat never started"
                ws2.send_text("second")
                resp = recv(ws2)
                assert resp["type"] == "error"
                assert "already working" in resp["content"]
                assert recv(ws2)["type"] == "end"
                release.set()
                assert recv(ws1)["type"] == "text"
                assert recv(ws1)["type"] == "end"
    _cleanup_project("proj-locktest")


def test_rejected_chat_does_not_replace_cancel_handle(tmp_path, monkeypatch):
    """A rejected second prompt must not overwrite the first task handle.

    This covers the race where the old implementation created a rejection
    task and assigned it to ``sess.chat_task`` before observing the lock.
    """
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    started = threading.Event()

    async def slow_chat(message, session_id="default"):
        started.set()
        await asyncio.sleep(60)
        yield {"type": "text", "content": "never reached"}

    with patch("main.agent_core.chat", side_effect=slow_chat):
        with client.websocket_connect("/ws") as ws1:
            with client.websocket_connect("/ws") as ws2:
                for ws in (ws1, ws2):
                    ws.send_text(json.dumps({"project": "proj-admission-race"}))
                    assert ws.receive_json()["type"] == "history"
                ws1.send_text("first")
                assert started.wait(timeout=5), "first chat never started"
                ws2.send_text("second")
                assert recv(ws2)["type"] == "error"
                assert recv(ws2)["type"] == "end"
                # Cancellation from the second bound connection must still
                # target the original task.
                ws2.send_text(json.dumps({"type": "cancel"}))
                resp = recv(ws1)
                assert resp == {"type": "error", "content": "Run cancelled by user."}
                assert recv(ws1)["type"] == "end"
    _cleanup_project("proj-admission-race")


def test_bounded_outbox_marks_dropped_stream_and_keeps_controls():
    """Stream loss is bounded and visible; terminal frames are retained."""
    from main import _BoundedOutbox

    async def scenario():
        outbox = _BoundedOutbox(asyncio.get_running_loop(), maxsize=4,
                                control_reserve=1)
        for i in range(3):
            assert outbox.put_nowait({"type": "text", "content": str(i)})
        # The reserved slot causes this stream frame to be dropped and a gap
        # marker to be queued.
        assert not outbox.put_nowait({"type": "text", "content": "dropped"})
        assert outbox.put_nowait({"type": "end"})
        frames = [await outbox.get() for _ in range(4)]
        assert any(frame["type"] == "outbox_gap" and frame["dropped"] == 1
                   for frame in frames)
        assert any(frame["type"] == "end" for frame in frames)

    asyncio.run(scenario())


def test_chats_on_different_sessions_run_concurrently(tmp_path, monkeypatch):
    """The session lock must not serialize chats across DIFFERENT projects."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    both_started = threading.Event()
    release = threading.Event()
    state = {"started": 0}

    async def slow_chat(message, session_id="default"):
        state["started"] += 1
        if state["started"] == 2:
            both_started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        yield {"type": "text", "content": f"done {session_id}"}

    with patch("main.agent_core.chat", side_effect=slow_chat):
        with client.websocket_connect("/ws") as ws1:
            with client.websocket_connect("/ws") as ws2:
                for ws, sid in ((ws1, "proj-conc-a"), (ws2, "proj-conc-b")):
                    ws.send_text(json.dumps({"project": sid}))
                    assert ws.receive_json()["type"] == "history"
                ws1.send_text("one")
                ws2.send_text("two")
                # Both chats must be in flight at the same time.
                assert both_started.wait(timeout=5), "sessions were serialized"
                release.set()
                assert recv(ws1)["type"] == "text"
                assert recv(ws1)["type"] == "end"
                assert recv(ws2)["type"] == "text"
                assert recv(ws2)["type"] == "end"
    _cleanup_project("proj-conc-a")
    _cleanup_project("proj-conc-b")


def test_project_state_frame_on_join(tmp_path, monkeypatch):
    """Joining a project replays the history frame, then a project_state frame
    restoring the Verilog viewer and PPA metric cards from persisted state."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    sid = "proj-statetest"
    agent_core._histories[sid] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "build it"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "run_silicon_compiler",
                          "arguments": json.dumps({"verilog_code": "module m; endmodule",
                                                   "top_module": "m"})}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "Synthesis successful!\ncellarea: 123.4\nsetupslack: 0.5"},
        {"role": "assistant", "content": "All done."},
    ]
    agent_core._project_names[sid] = "State test"
    agent_core._save_project(sid)
    try:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"project": sid}))
            assert ws.receive_json()["type"] == "history"
            frame = ws.receive_json()
            assert frame["type"] == "project_state"
            assert frame["verilog"] == {"top": "m", "code": "module m; endmodule"}
            # Metric values are numeric (the fmax Hz→MHz conversion requires
            # it); the frontend formats for display.
            assert frame["metrics"]["cellarea"] == 123.4
            assert frame["metrics"]["setupslack"] == 0.5
    finally:
        _cleanup_project(sid)


def test_project_state_frame_omitted_when_empty(tmp_path, monkeypatch):
    """A project with no persisted dashboard state sends no project_state."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    sid = "proj-emptystate"
    agent_core._histories[sid] = [{"role": "user", "content": "hi"}]
    agent_core._project_names[sid] = "Empty"
    agent_core._save_project(sid)
    try:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"project": sid}))
            assert ws.receive_json()["type"] == "history"
            assert agent_core.get_project_state(sid) == {}
    finally:
        _cleanup_project(sid)


def test_rejoin_mid_run_gets_busy_and_buffered_events(tmp_path, monkeypatch):
    """A client joining a project while a chat is mid-run (the page-refresh
    case) receives history, then a busy frame, then the run's buffered events,
    then the remaining live events. The originating client keeps receiving
    them too (fan-out to both connections)."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    proceed = threading.Event()

    async def gated_chat(message, session_id="default"):
        yield {"type": "text", "content": "working on it"}
        while not proceed.is_set():
            await asyncio.sleep(0.01)
        yield {"type": "text", "content": "finished"}

    with patch("main.agent_core.chat", side_effect=gated_chat):
        with client.websocket_connect("/ws") as ws1:
            ws1.send_text(json.dumps({"project": "proj-fanout"}))
            assert ws1.receive_json()["type"] == "history"
            ws1.send_text("start")
            assert recv(ws1) == {"type": "text", "content": "working on it"}
            # Second client joins the same project while the run is in flight
            # (equivalent to a page refresh: the buffer, not the connection,
            # is the source of truth for chat events).
            with client.websocket_connect("/ws") as ws2:
                ws2.send_text(json.dumps({"project": "proj-fanout"}))
                assert ws2.receive_json()["type"] == "history"
                assert recv(ws2)["type"] == "busy"
                assert recv(ws2) == {"type": "text", "content": "working on it"}
                proceed.set()
                assert recv(ws2) == {"type": "text", "content": "finished"}
                assert recv(ws2)["type"] == "end"
            # The original connection saw the same live events (fan-out).
            assert recv(ws1) == {"type": "text", "content": "finished"}
            assert recv(ws1)["type"] == "end"
    _cleanup_project("proj-fanout")


def test_rejoin_mid_run_replays_reasoning_deltas(tmp_path, monkeypatch):
    """Provider reasoning is live-only in persisted history, but a client that
    rejoins mid-run (the page-refresh case) still receives it: the session
    buffer replays the buffered generation_start and reasoning deltas, then
    live reasoning deltas keep flowing to the rebound connection."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    proceed = threading.Event()

    async def gated_chat(message, session_id="default"):
        yield {"type": "generation_start", "round": 1, "generation_id": "gen-r"}
        yield {"type": "reasoning_delta", "content": "thinking part one"}
        while not proceed.is_set():
            await asyncio.sleep(0.01)
        yield {"type": "reasoning_delta", "content": " thinking part two"}
        yield {"type": "text", "content": "done"}

    with patch("main.agent_core.chat", side_effect=gated_chat):
        with client.websocket_connect("/ws") as ws1:
            ws1.send_text(json.dumps({"project": "proj-reason-rejoin"}))
            assert ws1.receive_json()["type"] == "history"
            ws1.send_text("start")
            assert recv(ws1)["type"] == "generation_start"
            assert recv(ws1) == {"type": "reasoning_delta",
                                 "content": "thinking part one"}
            # Page refresh: a second client joins while the run is in flight.
            with client.websocket_connect("/ws") as ws2:
                ws2.send_text(json.dumps({"project": "proj-reason-rejoin"}))
                assert ws2.receive_json()["type"] == "history"
                assert recv(ws2)["type"] == "busy"
                # The buffered reasoning reaches the refreshed client...
                # (raw read: recv() strips the ts stamp for equality asserts)
                replayed_start = ws2.receive_json()
                assert replayed_start["type"] == "generation_start"
                # Chat frames carry a wall-clock stamp (discreet console
                # timestamps); it survives the buffer replay.
                assert isinstance(replayed_start.get("ts"), (int, float))
                assert recv(ws2) == {"type": "reasoning_delta",
                                     "content": "thinking part one"}
                proceed.set()
                # ...and live deltas continue to flow after the rebind.
                assert recv(ws2) == {"type": "reasoning_delta",
                                     "content": " thinking part two"}
                assert recv(ws2) == {"type": "text", "content": "done"}
                assert recv(ws2)["type"] == "end"
    _cleanup_project("proj-reason-rejoin")


def test_join_idle_project_sends_no_busy(tmp_path, monkeypatch):
    """Joining a project with no chat in flight sends history (+ optional
    project_state) and nothing else — no busy frame, no event replay."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    sid = "proj-idlejoin"
    agent_core._histories[sid] = [{"role": "user", "content": "hi"}]
    agent_core._project_names[sid] = "Idle"
    agent_core._save_project(sid)
    try:
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"project": sid}))
            assert ws.receive_json()["type"] == "history"
            # A chat on the idle session must run normally (no stale lock).
            async def quick_chat(message, session_id="default"):
                yield {"type": "text", "content": "ok"}

            with patch("main.agent_core.chat", side_effect=quick_chat):
                ws.send_text("go")
                assert recv(ws) == {"type": "text", "content": "ok"}
                assert recv(ws)["type"] == "end"
    finally:
        _cleanup_project(sid)


def test_cancel_from_another_bound_connection(tmp_path, monkeypatch):
    """The stop button works from ANY client bound to the project — including
    one that joined after the run started (the page-refresh case): the chat
    task handle lives on the session, not on the originating connection."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    started = threading.Event()

    async def gated_chat(message, session_id="default"):
        started.set()
        await asyncio.sleep(60)  # cancelled long before this
        yield {"type": "text", "content": "never reached"}

    with patch("main.agent_core.chat", side_effect=gated_chat):
        with client.websocket_connect("/ws") as ws1:
            ws1.send_text(json.dumps({"project": "proj-stop"}))
            assert ws1.receive_json()["type"] == "history"
            ws1.send_text("start")
            assert started.wait(timeout=5), "chat never started"
            # Second client joins the same project mid-run (page refresh) and
            # hits stop — the run must be cancelled even though THIS
            # connection never started a chat itself.
            with client.websocket_connect("/ws") as ws2:
                ws2.send_text(json.dumps({"project": "proj-stop"}))
                assert ws2.receive_json()["type"] == "history"
                assert ws2.receive_json()["type"] == "busy"
                ws2.send_text(json.dumps({"type": "cancel"}))
                resp = recv(ws2)
                assert resp["type"] == "error" and resp["content"] == "Run cancelled by user."
                assert recv(ws2)["type"] == "end"
            # The originating connection gets the same frames (session buffer).
            resp = recv(ws1)
            assert resp["type"] == "error" and resp["content"] == "Run cancelled by user."
            assert recv(ws1)["type"] == "end"
    _cleanup_project("proj-stop")


def test_job_started_broadcast_during_chat(tmp_path, monkeypatch):
    """A job submitted by a tool mid-chat produces a job_started frame (with
    the owning session) to the bound connection, before the chat's own
    frames — this is how the frontend learns the job_id early enough to
    watch the log live."""
    import jobs
    from jobs import JobManager

    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    mgr = JobManager(tmp_path / "jobs.db", tmp_path / "joblogs")
    monkeypatch.setattr(jobs, "manager", mgr)

    async def fake_chat(message, session_id="default"):
        async def runner(job):
            await job.emit("hello line")
            return "ok"
        job = jobs.manager.submit("synthesis", {}, runner=runner, design="m")
        await job.wait()
        yield {"type": "text", "content": "done"}

    with patch("main.agent_core.chat", side_effect=fake_chat):
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"project": "proj-jobsbc"}))
            assert ws.receive_json()["type"] == "history"
            ws.send_text("go")
            frame = recv(ws)
            assert frame["type"] == "job_started"
            assert frame["kind"] == "synthesis"
            assert frame["design"] == "m"
            assert frame["session"] is None  # submitted without a tool context
            assert recv(ws) == {"type": "text", "content": "done"}
            assert recv(ws)["type"] == "end"
    _cleanup_project("proj-jobsbc")
