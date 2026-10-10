"""JobManager: decouples long-running EDA work from the WebSocket lifecycle.

Jobs (simulation, synthesis) run as asyncio tasks independent of any client
connection, so they keep running when the browser disconnects. Clients attach
over the WS protocol ({"type": "subscribe"} / {"type": "cancel_job"} frames,
see main.py) and replay missed output from an in-memory ring buffer; every
line is also appended to a per-job disk log under data/jobs/<job_id>/job.log.

Persistence is SQLite via aiosqlite at backend/data/jobs.db (NOT under
SC_BUILDS). On startup, jobs recorded as running whose process group is gone
are reconciled to 'interrupted'.

Tool-facing pattern (see eda_tools.run_simulation / run_silicon_compiler):

    async def runner(job):
        rc, out = await job.run_process(cmd, cwd=..., timeout=120)  # streamed
        return f"result text ({rc})"
    job = manager.submit("simulation", {"top_module": top}, runner=runner, design=top)
    result = await job.wait()   # final result string; shielded from caller cancel

The agent-visible tool result appends a "job_id: <id>" marker line so the
frontend can subscribe to the job's live log.
"""
import asyncio
import json
import logging
import os
import signal
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger("jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
  design TEXT, jobname TEXT, params TEXT,
  pid INTEGER,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  returncode INTEGER, error TEXT,
  build_dir TEXT, manifest_path TEXT, log_path TEXT,
  session TEXT,
  outcome TEXT,
  netlist_sha256 TEXT,
  timing_paths_sha256 TEXT,
  layout_view_sha256 TEXT
);
"""

TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "interrupted"})

RING_MAX = 2000        # per-job in-memory (seq, line) ring buffer
SUB_QUEUE_MAX = 500    # per-subscriber bounded queue (backpressure)
KILL_GRACE_S = 10      # SIGTERM -> SIGKILL escalation delay on cancel/timeout


class JobCancelledError(Exception):
    """Raised into job.wait() when the job was cancelled."""


class JobTimeoutError(Exception):
    """Raised by Job.run_process when the step exceeded its timeout (the
    process tree has already been killed)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pgid_alive(pgid: int) -> bool:
    try:
        os.kill(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def _kill_process_tree(proc) -> None:
    """SIGTERM the child's process group, escalate to SIGKILL after 10 s.
    The child was spawned with start_new_session=True, so pgid == child pid."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_S)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()


def _offer(q: asyncio.Queue, item: tuple, gap_seq: int | None = None) -> None:
    """Non-blocking enqueue with drop-oldest backpressure. When *gap_seq* is
    given (queue was full), a ("gap", seq) marker precedes the item so the
    subscriber can emit a log_gap frame."""
    needed = 2 if gap_seq is not None else 1
    while q.qsize() > SUB_QUEUE_MAX - needed:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
    if gap_seq is not None:
        q.put_nowait(("gap", gap_seq))
    q.put_nowait(item)


class Job:
    """One unit of EDA work. Created by JobManager.submit; the runner coroutine
    drives it via run_process()/emit() and returns the final result string."""

    def __init__(self, manager: "JobManager", job_id: str, kind: str,
                 params: dict, design: str | None, jobname: str | None,
                 session: str | None = None):
        self.manager = manager
        self.id = job_id
        self.kind = kind
        self.params = params
        self.design = design
        self.jobname = jobname or "job0"
        self.session = session  # owning chat session (project) for UI filtering
        self.status = "queued"
        # Lifecycle and domain result are deliberately separate.
        self.outcome: str | None = None
        self.seq = 0
        self.ring: deque[tuple[int, str]] = deque(maxlen=RING_MAX)
        self.subs: set[asyncio.Queue] = set()
        self.proc = None            # current asyncio subprocess, if any
        self.pgid: int | None = None
        self.returncode: int | None = None
        self.error: str | None = None
        self.manifest: dict | None = None      # sent with the final job_status frame
        self.manifest_path: str | None = None
        self.build_dir: str | None = None
        self.cancel_requested = False
        self._cancel_delivered = False
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.log_path = str(manager.jobs_root / job_id / "job.log")
        self._log_fp = None
        self._future: asyncio.Future | None = None
        # Completed jobs retain their result so the event-loop-bound Future
        # can be released.  The manager keeps finished jobs in its registry
        # for history, and retaining every Future/task there pins old
        # asyncio loops when callers use repeated ``asyncio.run`` sessions.
        self._result: str | None = None
        self._failure: BaseException | None = None
        self.task: asyncio.Task | None = None

    async def emit(self, line: str) -> None:
        """Append one output line to the ring buffer, the disk log, and every
        subscriber queue (drop-oldest + gap marker on overflow)."""
        self.seq += 1
        seq = self.seq
        self.ring.append((seq, line))
        if self._log_fp is None:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            self._log_fp = open(self.log_path, "a", buffering=1)  # line-buffered
        self._log_fp.write(line + "\n")
        for q in list(self.subs):
            _offer(q, ("line", seq, line), gap_seq=seq if q.full() else None)

    async def run_process(self, cmd: list[str], cwd: str | None = None,
                          timeout: float | None = None,
                          env: dict[str, str] | None = None) -> tuple[int, str]:
        """Spawn *cmd* as its own process-group leader and stream merged
        stdout/stderr line by line into the job log. Returns (returncode,
        full output). Raises JobTimeoutError (tree killed) on timeout and
        re-raises asyncio.CancelledError (tree killed) on job cancel."""
        if self.cancel_requested:
            raise asyncio.CancelledError()
        await self.emit("$ " + " ".join(cmd))
        # Spawn via a shielded task: a cancel delivered mid-spawn must not
        # orphan the child — retrieve the handle and kill the tree instead.
        create = asyncio.ensure_future(asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            cwd=cwd,
            env={**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"},
        ))
        try:
            proc = await asyncio.shield(create)
        except asyncio.CancelledError:
            try:
                proc = await create  # spawn finishes shortly; get the handle
            except Exception:
                raise asyncio.CancelledError() from None
            await _kill_process_tree(proc)
            raise
        self.proc = proc
        self.pgid = proc.pid  # start_new_session -> child is the group leader
        await self.manager._update_job(self, pid=self.pgid)
        lines: list[str] = []

        async def _reader() -> None:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\r\n")
                lines.append(line)
                await self.emit(line)

        try:
            if timeout is None:
                await _reader()
            else:
                await asyncio.wait_for(_reader(), timeout)
        except asyncio.TimeoutError:
            await _kill_process_tree(proc)
            raise JobTimeoutError(f"timed out after {timeout}s")
        except asyncio.CancelledError:
            await _kill_process_tree(proc)
            raise
        await proc.wait()  # always reap
        self.proc = None
        self.returncode = proc.returncode
        return proc.returncode, "\n".join(lines)

    async def wait(self) -> str:
        """Await the runner's result string. Shielded: if the awaiting caller
        (e.g. the chat task) is cancelled, the job itself keeps running."""
        future = self._future
        if future is not None:
            return await asyncio.shield(future)
        if self.status == "done":
            return self._result or ""
        if self.status == "failed" and self._failure is not None:
            raise self._failure
        raise JobCancelledError(self.id)

    def set_outcome(self, outcome: str) -> None:
        if outcome not in {"success", "failure", "inconclusive"}:
            raise ValueError(f"invalid job outcome: {outcome}")
        self.outcome = outcome

    def _notify_status(self) -> None:
        for q in list(self.subs):
            _offer(q, ("status", self.status, self.manifest, self.outcome))

    def _close_log(self) -> None:
        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except OSError:
                pass
            self._log_fp = None


class JobManager:
    """In-process asyncio job registry. One singleton (`jobs.manager`) per
    backend; tests construct isolated instances on tmp paths."""

    def __init__(self, db_path: str | Path, jobs_root: str | Path):
        self.db_path = str(db_path)
        self.jobs_root = Path(jobs_root)
        self.jobs: dict[str, Job] = {}
        self.listeners: set = set()  # websockets receiving job_started broadcasts
        self._db: aiosqlite.Connection | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def __del__(self):
        """Stop an abandoned aiosqlite worker during best-effort teardown.

        The application lifespan calls :meth:`stop` explicitly.  A manager
        created by an isolated caller can outlive the event loop that started
        it, though, especially when a synchronous harness uses
        ``asyncio.run`` repeatedly.  ``aiosqlite.Connection.__del__`` warns
        when its worker is still attached at garbage collection and the
        worker can later try to notify a closed loop.  ``Connection.stop`` is
        deliberately synchronous and does not require an event loop, so it is
        safe for this last-resort path.  Normal async cleanup remains the
        authoritative path and waits for queued work to finish.
        """
        db = getattr(self, "_db", None)
        if db is None:
            return
        try:
            db.stop()
        except Exception:
            # Destructors must never surface teardown errors.
            pass
        self._db = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Open the DB and reconcile stale running jobs. Called from the app
        lifespan, and lazily on first use so tools also work without it."""
        await self._ensure_started()

    async def stop(self) -> None:
        """Cancel all in-memory running jobs (server shutdown) and close the DB."""
        for job in list(self.jobs.values()):
            if job.status not in TERMINAL_STATES:
                await self.cancel(job.id)
        # Wait for every job task — including ones that already delivered
        # their result but still owe the final status UPDATE — before closing.
        tasks = [j.task for j in self.jobs.values() if j.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None

    async def _ensure_started(self) -> None:
        loop = asyncio.get_running_loop()
        if self._db is not None and self._loop is loop:
            return
        lock = self._start_lock
        if lock is None or self._lock_loop is not loop:
            # aiosqlite connections and asyncio locks are loop-bound; tests
            # drive one event loop per test, so rebind when the loop changes.
            lock = self._start_lock = asyncio.Lock()
            self._lock_loop = loop
        async with lock:
            if self._db is not None and self._loop is loop:
                return
            if self._db is not None:
                try:
                    await self._db.close()
                except Exception:
                    pass
                self._db = None
            db_dir = os.path.dirname(self.db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            self.jobs_root.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.executescript(SCHEMA)
            # Migrate DBs created before the session column existed (the DB
            # persists on a bind mount): CREATE TABLE IF NOT EXISTS is a no-op
            # for them, so add the column explicitly when missing.
            cols = {row[1] for row in
                    await (await self._db.execute("PRAGMA table_info(jobs)")).fetchall()}
            if "session" not in cols:
                await self._db.execute("ALTER TABLE jobs ADD COLUMN session TEXT")
            if "outcome" not in cols:
                await self._db.execute("ALTER TABLE jobs ADD COLUMN outcome TEXT")
            if "netlist_sha256" not in cols:
                # Durable gate-netlist artifact link (Phase G1): survives the
                # in-memory manifest, so a restarted backend can still serve
                # the bounded graph of a previous run's synthesis.
                await self._db.execute("ALTER TABLE jobs ADD COLUMN netlist_sha256 TEXT")
            if "timing_paths_sha256" not in cols:
                # Durable timing-path artifact link (Phase G3), same contract.
                await self._db.execute("ALTER TABLE jobs ADD COLUMN timing_paths_sha256 TEXT")
            if "layout_view_sha256" not in cols:
                # Durable layout-view artifact link (Phase G4), same contract.
                await self._db.execute("ALTER TABLE jobs ADD COLUMN layout_view_sha256 TEXT")
            await self._db.commit()
            self._loop = loop
            await self._reconcile()

    async def _reconcile(self) -> None:
        """Jobs left 'running'/'queued' by a previous process whose recorded
        process group no longer exists are marked 'interrupted'."""
        cur = await self._db.execute(
            "SELECT id, pid FROM jobs WHERE status IN ('running', 'queued')")
        stale = []
        for row in await cur.fetchall():
            if row["pid"] is None or not _pgid_alive(row["pid"]):
                stale.append(row["id"])
        for job_id in stale:
            logger.info("job %s: stale running job -> interrupted", job_id)
            await self._db.execute(
                "UPDATE jobs SET status='interrupted', finished_at=? WHERE id=?",
                (_now(), job_id))
        if stale:
            await self._db.commit()

    # -- submission / cancellation ------------------------------------------

    def submit(self, kind: str, params: dict, *, runner, design: str | None = None,
               jobname: str | None = None, session: str | None = None) -> Job:
        """Register and start a job. *runner* is `async def runner(job) -> str`
        returning the result string delivered to job.wait() callers.
        *session* tags the job with the owning chat session (project) so the
        frontend can filter the global registry per project.
        Must be called from a running event loop."""
        job_id = f"{kind[:3]}-{uuid.uuid4().hex[:8]}"
        job = Job(self, job_id, kind, params, design, jobname, session=session)
        self.jobs[job_id] = job
        loop = asyncio.get_running_loop()
        job._future = loop.create_future()
        # Publish admission before scheduling the runner. The live listener
        # is what gives clients the id they need to subscribe, so a runner
        # that emits immediately must never beat the job_started frame to the
        # connection's outbox. (The broadcast remains task-based so a slow or
        # disconnected listener cannot block submission.)
        self._broadcast({"type": "job_started", "job_id": job_id,
                         "kind": kind, "design": design, "session": session})
        job.task = loop.create_task(self._run_job(job, runner))
        return job

    async def cancel(self, job_id: str) -> bool:
        """Cancel a live job. If a subprocess is in flight, kill its tree
        (SIGTERM, escalating to SIGKILL after 10 s in _kill_process_tree) and
        let the runner unwind through EOF — the task is NOT cancelled in that
        case, so the cancellation can't land mid-finalization and strand
        job.wait() callers. With no subprocess in flight (e.g. the synthesis
        worker thread), cancel the job task directly. Returns False for
        unknown job ids."""
        job = self.jobs.get(job_id)
        if job is None:
            return False
        if job.status in TERMINAL_STATES:
            return True
        job.cancel_requested = True
        if job.proc is not None:
            await _kill_process_tree(job.proc)
        elif not job._cancel_delivered and job.task is not None:
            job._cancel_delivered = True
            job.task.cancel()
        return True

    async def cancel_session(self, session: str) -> int:
        """Cancel all non-terminal jobs owned by *session*.

        This is used by the chat Stop action so a shielded ``job.wait()``
        cannot leave an EDA subprocess running after the agent is cancelled.
        The operation is idempotent and returns the number of jobs targeted.
        """
        owned = [job for job in self.jobs.values()
                 if job.session == session and job.status not in TERMINAL_STATES]
        if not owned:
            return 0
        results = await asyncio.gather(
            *(self.cancel(job.id) for job in owned), return_exceptions=True)
        for job, result in zip(owned, results):
            if isinstance(result, Exception):
                logger.error("job %s: session cancellation failed: %s",
                             job.id, result)
        return sum(1 for result in results if result is True)

    async def _run_job(self, job: Job, runner) -> None:
        await self._ensure_started()
        await self._insert_job(job)
        job.status = "running"
        job.started_at = _now()
        await self._update_job(job, status="running", started_at=job.started_at)
        job._notify_status()
        result, failure = None, None
        try:
            result = await runner(job)
            job.status = "cancelled" if job.cancel_requested else "done"
            if job.status == "done" and job.outcome is None:
                job.outcome = "success"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.cancel_requested = True
            logger.info("job %s cancelled", job.id)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            failure = e
            logger.exception("job %s failed", job.id)
        # Finalize in a shielded task: a cancel racing into this section must
        # not prevent the result future from being resolved — job.wait()
        # callers would otherwise hang forever.
        finalize = asyncio.ensure_future(self._finalize_job(job, result, failure))
        try:
            await asyncio.shield(finalize)
        except asyncio.CancelledError:
            await finalize
        # The registry keeps terminal jobs for history. Release the task's
        # loop reference once finalization has completed; ``wait`` uses the
        # retained result/error for any later callers.
        job.task = None

    async def _finalize_job(self, job: Job, result, failure) -> None:
        job.finished_at = _now()
        netlist_sha = None
        timing_paths_sha = None
        layout_view_sha = None
        manifest = job.manifest if isinstance(job.manifest, dict) else None
        if isinstance(manifest, dict):
            gate_meta = manifest.get("gate_netlist")
            if isinstance(gate_meta, dict) and isinstance(gate_meta.get("repository_sha256"), str):
                netlist_sha = gate_meta["repository_sha256"]
            paths_meta = manifest.get("timing_paths")
            if isinstance(paths_meta, dict) and isinstance(paths_meta.get("repository_sha256"), str):
                timing_paths_sha = paths_meta["repository_sha256"]
            layout_meta = manifest.get("layout_view")
            if isinstance(layout_meta, dict) and isinstance(layout_meta.get("repository_sha256"), str):
                layout_view_sha = layout_meta["repository_sha256"]
        try:
            await self._update_job(
                job, status=job.status, finished_at=job.finished_at,
                returncode=job.returncode, error=job.error,
                build_dir=job.build_dir, manifest_path=job.manifest_path,
                outcome=job.outcome, netlist_sha256=netlist_sha,
                timing_paths_sha256=timing_paths_sha,
                layout_view_sha256=layout_view_sha)
        except Exception:
            logger.exception("job %s: failed to persist final state", job.id)
        job._close_log()
        # Terminal status first, then release waiters: subscribers see the
        # final job_status before the tool_result/end frames of the chat.
        job._notify_status()
        if not job._future.done():
            if job.status == "done":
                job._result = result
                job._future.set_result(result)
            elif job.status == "failed":
                job._failure = failure
                job._future.set_exception(failure)
            else:
                job._future.set_exception(JobCancelledError(job.id))
        # All current waiters have been notified. Keeping this future in a
        # long-lived job registry would retain the event loop that created it.
        job._future = None

    # -- subscriptions --------------------------------------------------------

    def add_listener(self, ws) -> None:
        """Register a chat websocket for job_started broadcasts."""
        self.listeners.add(ws)

    def remove_listener(self, ws) -> None:
        self.listeners.discard(ws)

    def _broadcast(self, frame: dict) -> None:
        for ws in list(self.listeners):
            asyncio.get_running_loop().create_task(self._broadcast_one(ws, frame))

    async def _broadcast_one(self, ws, frame: dict) -> None:
        try:
            await ws.send_json(frame)
        except Exception:
            self.listeners.discard(ws)

    async def subscribe(self, job_id: str, ws, since_seq: int = 0) -> None:
        """Replay ring entries with seq > since_seq to *ws*, then stream live
        until the job reaches a terminal state (or the send side breaks).
        Runs as its own task per (websocket, job); a slow client only stalls
        its own task, never the log reader."""
        await self._ensure_started()
        job = self.jobs.get(job_id)
        if job is None:
            row = await self._fetch_job(job_id)
            if row is None:
                await self._safe_send(ws, {"type": "error",
                                           "content": f"unknown job_id: {job_id}"})
            else:
                # DB-only job (from a previous backend run): the ring buffer
                # and in-memory manifest are gone. Replay the surviving disk
                # log (seq = line number), then ship the final status.
                log_path = row["log_path"]
                if log_path and os.path.exists(log_path):
                    seq = 0
                    with open(log_path) as f:
                        for line in f:
                            seq += 1
                            if seq > since_seq:
                                if not await self._safe_send(ws, {
                                        "type": "tool_log", "job_id": job_id,
                                        "seq": seq, "line": line.rstrip("\r\n")}):
                                    return
                await self._safe_send(ws, {
                    "type": "job_status", "job_id": job_id,
                    "status": row["status"], "outcome": row["outcome"],
                    "manifest": None})
            return
        # Register before taking the replay snapshot. Otherwise output emitted
        # while replay is awaiting websocket I/O falls into the gap between
        # the ring iteration and ``job.subs.add(q)`` and is lost. Snapshot the
        # deque too: iterating the live deque while emit() appends can raise
        # ``RuntimeError: deque mutated during iteration``.
        q: asyncio.Queue = asyncio.Queue(maxsize=SUB_QUEUE_MAX)
        job.subs.add(q)
        replay_ring = list(job.ring)
        replay_upto = job.seq
        status_at_snapshot = job.status
        try:
            # Replay phase. If the ring overflowed past since_seq, tell the
            # client from which seq the log resumes.
            if replay_ring:
                oldest = replay_ring[0][0]
                if since_seq + 1 < oldest:
                    if not await self._safe_send(ws, {"type": "log_gap", "job_id": job_id,
                                                      "from_seq": oldest}):
                        return
                for seq, line in replay_ring:
                    if seq > since_seq:
                        if not await self._safe_send(ws, {"type": "tool_log", "job_id": job_id,
                                                          "seq": seq, "line": line}):
                            return

            async def send_item(item) -> bool:
                kind = item[0]
                if kind == "line":
                    # Lines emitted after the replay snapshot are delivered
                    # from q; lines at/before the cursor are already replayed.
                    if item[1] <= replay_upto:
                        return True
                    frame = {"type": "tool_log", "job_id": job_id,
                             "seq": item[1], "line": item[2]}
                elif kind == "gap":
                    frame = {"type": "log_gap", "job_id": job_id, "from_seq": item[1]}
                else:  # "status"
                    frame = {"type": "job_status", "job_id": job_id,
                             "status": item[1], "manifest": item[2],
                             "outcome": item[3]}
                if not await self._safe_send(ws, frame):
                    return False
                return not (kind == "status" and item[1] in TERMINAL_STATES)

            # Drain events emitted while the replay was in flight before
            # switching to the blocking live loop. Queue order preserves log
            # sequence/status ordering; duplicate lines are cursor-filtered.
            while True:
                try:
                    item = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not await send_item(item):
                    return

            # If the job was already terminal at snapshot time, no live
            # subscription is needed. The queue was still registered during
            # replay so a terminal transition racing the snapshot is drained.
            if status_at_snapshot in TERMINAL_STATES:
                if not await self._safe_send(ws, {"type": "job_status", "job_id": job_id,
                                                   "status": job.status, "outcome": job.outcome,
                                                   "manifest": job.manifest}):
                    return
                return

            # Live phase.
            while True:
                item = await q.get()
                if not await send_item(item):
                    break
        finally:
            job.subs.discard(q)

    @staticmethod
    async def _safe_send(ws, frame: dict) -> bool:
        try:
            await ws.send_json(frame)
            return True
        except Exception as e:
            logger.warning("send to subscriber failed (%s: %s); dropping subscription",
                           type(e).__name__, e)
            return False

    # -- persistence ----------------------------------------------------------

    async def list_jobs(self, limit: int = 100) -> list[dict]:
        await self._ensure_started()
        cur = await self._db.execute(
            "SELECT id, kind, status, outcome, design, created_at, started_at, "
            "finished_at, returncode, error, session "
            "FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in await cur.fetchall()]

    async def get_job(self, job_id: str) -> dict | None:
        """Return durable execution metadata plus any live structured manifest."""
        await self._ensure_started()
        row = await self._fetch_job(job_id)
        if row is None:
            return None
        detail = dict(row)
        try:
            detail["params"] = json.loads(detail.get("params") or "{}")
        except (TypeError, ValueError):
            detail["params"] = {}
        live = self.jobs.get(job_id)
        manifest = live.manifest if live is not None else None
        detail["manifest"] = manifest
        run_id = None
        if isinstance(manifest, dict):
            run_id = manifest.get("run_id")
            if run_id is None and isinstance(manifest.get("provenance"), dict):
                run_id = manifest["provenance"].get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            safe_id = run_id.strip()
            detail["evidence"] = {
                "run_id": safe_id,
                "manifest": f"/api/evidence/runs/{safe_id}",
                "report": f"/api/evidence/runs/{safe_id}/report",
                "assurance": f"/api/evidence/runs/{safe_id}/assurance",
                "implementation": f"/api/evidence/runs/{safe_id}/implementation",
            }
        return detail

    async def _fetch_job(self, job_id: str):
        cur = await self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        return await cur.fetchone()

    async def _insert_job(self, job: Job) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO jobs (id, kind, status, design, jobname, params,"
            " pid, created_at, started_at, finished_at, returncode, error,"
            " build_dir, manifest_path, log_path, session, outcome)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.id, job.kind, job.status, job.design, job.jobname,
             json.dumps(job.params), None, job.created_at, None, None, None,
             None, job.build_dir, None, job.log_path, job.session, job.outcome))
        await self._db.commit()

    async def _update_job(self, job: Job, **fields) -> None:
        if self._db is None:  # run_process can beat _run_job's first insert
            return
        allowed = {"status", "pid", "started_at", "finished_at", "returncode",
                   "error", "build_dir", "manifest_path", "outcome",
                   "netlist_sha256", "timing_paths_sha256",
                   "layout_view_sha256"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        clause = ", ".join(f"{k}=?" for k in sets)
        await self._db.execute(f"UPDATE jobs SET {clause} WHERE id=?",
                               (*sets.values(), job.id))
        await self._db.commit()


# Singleton used by main.py (lifespan + WS endpoint) and eda_tools.
_DEFAULT_DIR = Path(__file__).resolve().parent / "data"
manager = JobManager(_DEFAULT_DIR / "jobs.db", _DEFAULT_DIR / "jobs")
