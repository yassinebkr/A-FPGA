"""Tests for the JobManager (backend/jobs.py). Each test drives an isolated
manager on tmp paths via asyncio.run — no pytest-asyncio needed. Every manager
is stopped in a finally block: aiosqlite's worker thread is not a daemon, so
an unclosed connection would hang the pytest process at exit."""
import asyncio
import os
import subprocess
import sys

import pytest

import jobs as jobs_mod
from jobs import JobManager


def make_manager(tmp_path):
    return JobManager(tmp_path / "jobs.db", tmp_path / "jobs")


class FakeWS:
    """Minimal websocket stand-in: captures send_json frames."""

    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


def test_job_started_precedes_immediate_runner_output(tmp_path):
    async def main():
        mgr = make_manager(tmp_path)
        frames = []

        class Listener:
            async def send_json(self, frame):
                frames.append(frame)

        mgr.add_listener(Listener())
        try:
            async def runner(job):
                await job.emit("first output")
                return "ok"

            job = mgr.submit("simulation", {}, runner=runner, design="dut",
                             session="proj-live")
            assert await job.wait() == "ok"
            await asyncio.sleep(0)
            assert frames[0] == {
                "type": "job_started", "job_id": job.id,
                "kind": "simulation", "design": "dut",
                "session": "proj-live",
            }
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_submit_runs_trivial_job(tmp_path):
    async def main():
        mgr = make_manager(tmp_path)
        try:
            async def runner(job):
                rc, out = await job.run_process(
                    [sys.executable, "-c", "print('hello'); print('world')"])
                return f"rc={rc}\n{out}"

            job = mgr.submit("test", {"x": 1}, runner=runner, design="d")
            result = await job.wait()

            assert job.status == "done"
            assert job.returncode == 0
            assert "hello" in result and "world" in result
            # ring buffer captured the streamed lines
            ring_lines = [line for _, line in job.ring]
            assert "hello" in ring_lines and "world" in ring_lines
            # disk log under the job's dir
            log_text = open(job.log_path).read()
            assert "hello" in log_text and "world" in log_text
            # DB row
            rows = await mgr.list_jobs()
            assert rows[0]["id"] == job.id
            assert rows[0]["status"] == "done"
            assert rows[0]["kind"] == "test"
            assert rows[0]["design"] == "d"
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_completed_job_releases_event_loop_handles(tmp_path):
    """Retained job history must not pin the loop that created the job."""
    async def main():
        mgr = make_manager(tmp_path)
        try:
            async def runner(job):
                return "completed"

            job = mgr.submit("test", {}, runner=runner)
            assert await job.wait() == "completed"
            assert job._future is None
            # ``wait`` is released by the result future before the runner
            # task gets its final scheduling turn to clear its registry ref.
            await asyncio.sleep(0)
            assert job.task is None
            # A later caller still receives the durable in-memory result.
            assert await job.wait() == "completed"
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_job_detail_exposes_evidence_only_from_manifest_run_id(tmp_path):
    async def main():
        mgr = make_manager(tmp_path)
        try:
            async def runner(job):
                job.manifest = {"run_id": "run-verified", "target": "sky130"}
                return "ok"
            job = mgr.submit("synthesis", {"target": "sky130"}, runner=runner, design="core")
            await job.wait()
            detail = await mgr.get_job(job.id)
            assert detail["params"] == {"target": "sky130"}
            assert detail["evidence"]["report"] == "/api/evidence/runs/run-verified/report"

            async def no_manifest(other):
                return "ok"
            other = mgr.submit("simulation", {}, runner=no_manifest, design="core")
            await other.wait()
            assert "evidence" not in await mgr.get_job(other.id)
            assert await mgr.get_job("missing") is None
        finally:
            await mgr.stop()
    asyncio.run(main())


def test_subscribe_replays_ring_since_seq(tmp_path):
    async def main():
        mgr = make_manager(tmp_path)
        try:
            async def runner(job):
                await job.run_process(
                    [sys.executable, "-c", "for i in range(10): print(f'line{i}')"])
                return "ok"

            job = mgr.submit("test", {}, runner=runner)
            await job.wait()

            # seq 1 is the "$ ..." command echo, seqs 2..11 are line0..line9
            ws = FakeWS()
            await mgr.subscribe(job.id, ws, since_seq=5)
            logs = [f for f in ws.frames if f["type"] == "tool_log"]
            assert [f["seq"] for f in logs] == list(range(6, 12))
            assert logs[0]["line"] == "line4"
            assert all(f["job_id"] == job.id for f in logs)
            # a finished job immediately closes with its final status
            assert ws.frames[-1]["type"] == "job_status"
            assert ws.frames[-1]["status"] == "done"
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_subscribe_streams_live_until_done(tmp_path):
    async def main():
        mgr = make_manager(tmp_path)
        try:
            code = ("import time\n"
                    "for i in range(5):\n"
                    "    print(f'l{i}', flush=True)\n"
                    "    time.sleep(0.05)\n")

            async def runner(job):
                await job.run_process([sys.executable, "-c", code])
                return "ok"

            job = mgr.submit("test", {}, runner=runner)
            ws = FakeWS()
            sub = asyncio.create_task(mgr.subscribe(job.id, ws, since_seq=0))
            result = await job.wait()
            assert result == "ok"
            await asyncio.wait_for(sub, 5)  # subscribe task ends on terminal status

            logs = [f for f in ws.frames if f["type"] == "tool_log"]
            assert [f["line"] for f in logs if f["line"].startswith("l")] == \
                [f"l{i}" for i in range(5)]
            assert ws.frames[-1]["type"] == "job_status"
            assert ws.frames[-1]["status"] == "done"
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_subscribe_does_not_drop_lines_emitted_during_replay(tmp_path):
    """A slow replay websocket must still receive lines emitted meanwhile."""
    async def main():
        mgr = make_manager(tmp_path)
        try:
            replay_started = asyncio.Event()
            release_replay = asyncio.Event()
            allow_finish = asyncio.Event()

            class SlowWS(FakeWS):
                async def send_json(self, frame):
                    self.frames.append(frame)
                    if frame.get("type") == "tool_log" and frame.get("line") == "before":
                        replay_started.set()
                        await release_replay.wait()

            async def runner(job):
                await job.emit("before")
                await allow_finish.wait()
                return "ok"

            job = mgr.submit("test", {}, runner=runner)
            await asyncio.sleep(0)
            ws = SlowWS()
            sub = asyncio.create_task(mgr.subscribe(job.id, ws, since_seq=0))
            await asyncio.wait_for(replay_started.wait(), 5)
            # This line is emitted while subscribe() is awaiting the replay
            # websocket send. The old implementation had not registered its
            # queue yet, so it was lost (and could mutate the deque iterator).
            await job.emit("during")
            release_replay.set()
            allow_finish.set()
            await job.wait()
            await asyncio.wait_for(sub, 5)

            logs = [f["line"] for f in ws.frames if f["type"] == "tool_log"]
            assert logs == ["before", "during"]
            assert ws.frames[-1]["type"] == "job_status"
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_cancel_kills_sleep_process_tree(tmp_path):
    async def main():
        mgr = make_manager(tmp_path)
        try:
            # Parent python spawns a grandchild sleep, then sleeps itself: the
            # whole tree lives in the child's process group (start_new_session).
            code = ("import subprocess, time\n"
                    "subprocess.Popen(['sleep', '60'])\n"
                    "time.sleep(60)\n")

            async def runner(job):
                await job.run_process([sys.executable, "-c", code])
                return "unreachable"

            job = mgr.submit("test", {}, runner=runner)
            for _ in range(100):
                if job.pgid:
                    break
                await asyncio.sleep(0.05)
            assert job.pgid, "subprocess never started"

            await mgr.cancel(job.id)
            with pytest.raises(jobs_mod.JobCancelledError):
                await job.wait()
            assert job.status == "cancelled"
            # the whole process group is gone (SIGTERM, no 10s escalation needed)
            with pytest.raises(ProcessLookupError):
                os.kill(job.pgid, 0)
        finally:
            await mgr.stop()

    asyncio.run(main())


def _dead_pgid() -> int:
    """A pid that definitely no longer exists (spawned, exited, reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert proc.returncode == 0
    return proc.pid


def test_startup_reconciliation_marks_stale_jobs_interrupted(tmp_path):
    async def seed():
        mgr = make_manager(tmp_path)
        try:
            await mgr.start()
            await mgr._db.execute(
                "INSERT INTO jobs (id, kind, status, pid, created_at)"
                " VALUES ('stale1', 'test', 'running', ?, '2026-01-01T00:00:00+00:00')",
                (_dead_pgid(),))
            await mgr._db.execute(
                "INSERT INTO jobs (id, kind, status, pid, created_at)"
                " VALUES ('live1', 'test', 'running', ?, '2026-01-01T00:00:00+00:00')",
                (os.getpid(),))
            await mgr._db.commit()
        finally:
            await mgr.stop()

    asyncio.run(seed())

    async def check():
        mgr = make_manager(tmp_path)
        try:
            await mgr.start()  # reconciliation happens here
            rows = {row["id"]: row for row in await mgr.list_jobs()}
            assert rows["stale1"]["status"] == "interrupted"
            assert rows["live1"]["status"] == "running"
        finally:
            await mgr.stop()

    asyncio.run(check())


def test_session_tag_round_trips(tmp_path):
    """Jobs submitted with a session tag expose it via list_jobs (GET
    /api/jobs) so the frontend can filter the registry per project."""
    async def main():
        mgr = make_manager(tmp_path)
        try:
            async def runner(job):
                return "ok"

            job_a = mgr.submit("test", {}, runner=runner, session="proj-aaa")
            job_b = mgr.submit("test", {}, runner=runner, session="proj-bbb")
            await job_a.wait()
            await job_b.wait()
            rows = {row["id"]: row for row in await mgr.list_jobs()}
            assert rows[job_a.id]["session"] == "proj-aaa"
            assert rows[job_b.id]["session"] == "proj-bbb"
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_domain_outcome_is_separate_from_lifecycle_status(tmp_path):
    async def main():
        mgr = make_manager(tmp_path)
        try:
            async def runner(job):
                job.set_outcome("failure")
                return "Synthesis failed"

            job = mgr.submit("synthesis", {}, runner=runner, design="m")
            assert await job.wait() == "Synthesis failed"
            assert job.status == "done"
            assert job.outcome == "failure"
            row = next(r for r in await mgr.list_jobs() if r["id"] == job.id)
            assert row["status"] == "done"
            assert row["outcome"] == "failure"

            ws = FakeWS()
            await mgr.subscribe(job.id, ws)
            assert ws.frames[-1]["status"] == "done"
            assert ws.frames[-1]["outcome"] == "failure"
        finally:
            await mgr.stop()

    asyncio.run(main())


def test_session_column_migration(tmp_path):
    """A jobs DB created before the session column existed gets it via ALTER
    TABLE on startup (CREATE TABLE IF NOT EXISTS is a no-op for old DBs)."""
    import sqlite3
    db_path = tmp_path / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
        " status TEXT NOT NULL, design TEXT, jobname TEXT, params TEXT,"
        " pid INTEGER, created_at TEXT NOT NULL, started_at TEXT,"
        " finished_at TEXT, returncode INTEGER, error TEXT, build_dir TEXT,"
        " manifest_path TEXT, log_path TEXT)")  # the pre-session schema
    conn.execute("INSERT INTO jobs (id, kind, status, created_at)"
                 " VALUES ('old1', 'test', 'done', '2026-01-01')")
    conn.commit()
    conn.close()

    async def main():
        mgr = make_manager(tmp_path)
        try:
            await mgr.start()  # migration happens here
            async def runner(job):
                return "ok"
            job = mgr.submit("test", {}, runner=runner, session="proj-new")
            await job.wait()
            rows = {row["id"]: row for row in await mgr.list_jobs()}
            assert rows["old1"]["session"] is None  # pre-migration row
            assert rows[job.id]["session"] == "proj-new"
        finally:
            await mgr.stop()

    asyncio.run(main())
