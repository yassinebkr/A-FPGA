import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Load full-stack configuration before importing modules that resolve provider
# or EDA settings. Root .env is primary; backend/.env is a legacy fallback.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(os.path.dirname(_BACKEND_DIR), ".env"))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))
# Wizard-saved keys live in backend/data (bind-mounted in docker-compose, so
# they survive a `docker compose down/up`; the image-layer .env does not).
# Applied after the .env files with setdefault semantics: a hand-written .env
# keeps precedence. Imported before the provider modules for this ordering.
import mistral_setup
mistral_setup.load_provider_keys()

import agent_core
import gate_netlist
import timing_paths
import physical_layout
from evidence_api import router as evidence_router
from verification_api import router as verification_router
from tapeout_api import router as tapeout_router
import jobs
import model_registry
from genui import ContractProtocolError
from hardware_project import HardwareProjectError, HardwareProjectUpdate
from logging_config import setup_logging

setup_logging()
logger = logging.getLogger("server")

# Strong references to chat tasks whose client disconnected mid-run. asyncio
# holds only weak refs to tasks, so a task referenced solely by the (dead)
# endpoint frame would be garbage-collected — i.e. cancelled — mid-run.
_DETACHED_CHATS: set[asyncio.Task] = set()

# Per-session chat state: the serialization lock PLUS a bounded event buffer
# with subscribers. The lock stops a refreshed browser from starting a second
# agent loop on the same project; the buffer is the source of truth for chat
# frames, so a client that (re)joins mid-run — e.g. after a page refresh while
# a detached chat is still running — sees the run's liveness and events
# instead of a silent, stuck-looking screen. Chat frames flow through this
# buffer; job/subscription frames keep flowing through the per-connection
# outbox directly (the two meet in the outbox queue; their relative ordering
# is not guaranteed, but job logs carry their own seq cursors).
_CHAT_BUF_MAX = 1000
_OUTBOX_MAX = 512
_OUTBOX_CONTROL_RESERVE = 32

# S3 durable run snapshots use the existing public timeline as their storage.
_RUN_SNAPSHOT_PREFIX = "run-snapshot:"
_RUN_SNAPSHOT_VERSION = 1


def _live_run_state(session_id: str) -> dict | None:
    """Return the current agent-owned snapshot for websocket publication."""
    try:
        state = agent_core._get_state(session_id)
        snapshot = state.get("run_snapshot") if isinstance(state, dict) else None
        return dict(snapshot) if isinstance(snapshot, dict) else None
    except Exception:
        logger.exception("[%s] failed to read live run snapshot", session_id)
        return None


def _snapshot_event(snapshot: dict) -> dict:
    """Encode a bounded run snapshot in the existing public timeline schema."""
    run_id = str(snapshot.get("run_id") or "unknown")
    compact = {
        "version": _RUN_SNAPSHOT_VERSION, "run_id": run_id,
        "seq": int(snapshot.get("seq") or 0), "state": str(snapshot.get("state") or "running"),
        "phase": str(snapshot.get("phase") or "run"), "attempt": int(snapshot.get("attempt") or 0),
        "candidate": snapshot.get("candidate"), "best": snapshot.get("best"),
        "jobs": list(snapshot.get("jobs") or [])[:16], "terminal_reason": snapshot.get("terminal_reason"),
        "approval_pause": bool(snapshot.get("approval_pause")), "budgets": snapshot.get("budgets") or {},
    }
    # Keep JSON valid even when optional controller metadata is large.
    evidence = json.dumps(compact, separators=(",", ":"), default=str)
    if len(evidence) > 300:
        compact = {k: compact[k] for k in
                   ("version", "run_id", "seq", "state", "phase", "attempt",
                    "terminal_reason", "approval_pause")}
        evidence = json.dumps(compact, separators=(",", ":"), default=str)[:300]
    return {"type": "run_status", "content": {"event_id": _RUN_SNAPSHOT_PREFIX + run_id,
        "phase": compact["phase"], "status": compact["state"], "action": "run snapshot",
        "attempt": compact["attempt"], "evidence": evidence}}


def _snapshot_from_event(event: dict) -> dict | None:
    content = event.get("content") if isinstance(event, dict) else None
    if not isinstance(content, dict): return None
    event_id = str(content.get("event_id") or "")
    if not event_id.startswith(_RUN_SNAPSHOT_PREFIX): return None
    try: value = json.loads(content.get("evidence", "{}"))
    except (TypeError, ValueError): value = {}
    if not isinstance(value, dict): value = {}
    run_id = value.get("run_id") or event_id[len(_RUN_SNAPSHOT_PREFIX):]
    return {"version": value.get("version", _RUN_SNAPSHOT_VERSION), "run_id": str(run_id),
        "seq": int(value.get("seq") or 0), "state": str(value.get("state") or content.get("status") or "running"),
        "phase": str(value.get("phase") or content.get("phase") or "run"),
        "attempt": int(value.get("attempt") or content.get("attempt") or 0),
        "candidate": value.get("candidate"), "best": value.get("best"), "jobs": list(value.get("jobs") or [])[:16],
        "terminal_reason": value.get("terminal_reason"), "approval_pause": bool(value.get("approval_pause")),
        "budgets": value.get("budgets") or {}}


def _persist_run_snapshot(snapshot: dict) -> None:
    """Persist through agent_core's authoritative ``run_state`` snapshot."""
    session_id = snapshot.get("session_id")
    if not isinstance(session_id, str) or not session_id.startswith("proj-"):
        return
    try:
        get_state = getattr(agent_core, "_get_state", None)
        save = getattr(agent_core, "_save_project", None)
        if not callable(get_state) or not callable(save):
            return
        state = get_state(session_id)
        lifecycle = snapshot.get("state", "running")
        state["run_snapshot"] = {
            "version": _RUN_SNAPSHOT_VERSION, "run_id": snapshot.get("run_id"),
            "seq": int(snapshot.get("seq") or 0), "lifecycle": lifecycle,
            "phase": snapshot.get("phase"), "attempt": snapshot.get("attempt"),
            "candidate_ref": snapshot.get("candidate"), "best_ref": snapshot.get("best"),
            "budgets": snapshot.get("budgets") or {}, "jobs": list(snapshot.get("jobs") or [])[:32],
            "approval_pause": ({"pending": True, "reason": "awaiting_contract_approval"}
                               if snapshot.get("approval_pause") else None),
            "terminal_reason": snapshot.get("terminal_reason"),
        }
        save(session_id)
    except Exception:
        logger.exception("[%s] failed to persist run snapshot", session_id)


def _link_run_job(session_id: object, job_id: object) -> None:
    """Attach a newly submitted job to the authoritative bounded run snapshot."""
    if not (isinstance(session_id, str) and session_id.startswith("proj-")
            and isinstance(job_id, str) and job_id):
        return
    try:
        state = agent_core._get_state(session_id)
        snapshot = state.get("run_snapshot")
        if not isinstance(snapshot, dict):
            return
        linked = list(snapshot.get("jobs") or [])
        if job_id not in linked:
            linked.append(job_id)
            snapshot = dict(snapshot, jobs=linked[-32:])
            state["run_snapshot"] = snapshot
            agent_core._save_project(session_id)
    except Exception:
        logger.exception("[%s] failed to link job %s to run", session_id, job_id)


def _load_run_snapshot(session_id: str) -> dict | None:
    try:
        get_history = getattr(agent_core, "_get_history", None)
        if callable(get_history):
            get_history(session_id)  # loads persisted run_state and reconciles restart
        get_state = getattr(agent_core, "_get_state", None)
        state = get_state(session_id) if callable(get_state) else {}
        raw = state.get("run_snapshot") if isinstance(state, dict) else None
        if not isinstance(raw, dict):
            return None
        return {"version": raw.get("version", _RUN_SNAPSHOT_VERSION),
                "run_id": raw.get("run_id"), "seq": int(raw.get("seq") or 0),
                "state": raw.get("lifecycle", "running"), "phase": raw.get("phase"),
                "attempt": raw.get("attempt"), "candidate": raw.get("candidate_ref"),
                "best": raw.get("best_ref"), "budgets": raw.get("budgets") or {},
                "jobs": raw.get("jobs") or [],
                "approval_pause": bool(raw.get("approval_pause")),
                "terminal_reason": raw.get("terminal_reason")}
    except Exception:
        logger.exception("[%s] failed to load run snapshot", session_id)
        return None


def _is_critical_frame(frame: dict) -> bool:
    """Whether a frame must survive outbox backpressure."""
    return frame.get("type") in {
        "error", "end", "busy", "history", "project_state",
        "job_started", "job_status", "log_gap", "outbox_gap",
        "contract_state", "contract_error", "run_state",
    }


class _BoundedOutbox:
    """Bounded, single-sender queue with explicit stream-drop semantics.

    All mutations run on the connection event loop. Replayable stream frames
    are dropped once the reserved control capacity is reached and an
    ``outbox_gap`` marker records the loss. Critical frames evict an older
    replayable frame. If the queue is entirely critical frames, the explicit
    overflow callback closes the connection instead of silently dropping a
    terminal/control frame.
    """

    def __init__(self, loop, *, on_critical_overflow=None,
                 maxsize=_OUTBOX_MAX, control_reserve=_OUTBOX_CONTROL_RESERVE):
        self.loop = loop
        self.maxsize = maxsize
        self.control_reserve = min(control_reserve, maxsize - 1)
        self._items: deque[tuple[dict, bool]] = deque()
        self._wake = asyncio.Event()
        self._space = asyncio.Event()
        self._space.set()
        self._gap_frame: dict | None = None
        self._gap_pending = 0
        self._on_critical_overflow = on_critical_overflow
        self._closed = False

    def _critical_overflow(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger.error("closing websocket after critical outbox overflow")
        if self._on_critical_overflow is not None:
            self._on_critical_overflow()

    def _find_droppable(self):
        for index, (_, critical) in enumerate(self._items):
            if not critical:
                return index
        return None

    def _flush_gap(self) -> None:
        if not self._gap_pending or self._gap_frame is not None:
            return
        if len(self._items) >= self.maxsize:
            index = self._find_droppable()
            if index is None:
                return
            del self._items[index]
        marker = {"type": "outbox_gap", "dropped": self._gap_pending}
        self._gap_pending = 0
        self._gap_frame = marker
        self._items.append((marker, True))
        self._wake.set()
        if len(self._items) >= self.maxsize:
            self._space.clear()

    def _note_drop(self) -> None:
        if self._gap_frame is not None:
            self._gap_frame["dropped"] += 1
        else:
            self._gap_pending += 1
            self._flush_gap()

    def _try_put(self, frame: dict, critical: bool) -> bool:
        if self._closed:
            return False
        if not critical and len(self._items) >= self.maxsize - self.control_reserve:
            self._note_drop()
            return False
        if len(self._items) >= self.maxsize:
            index = self._find_droppable()
            if index is None:
                if critical:
                    return False
                self._note_drop()
                return False
            del self._items[index]
        self._items.append((frame, critical))
        self._wake.set()
        if len(self._items) >= self.maxsize:
            self._space.clear()
        return True

    def put_nowait(self, frame: dict, *, critical: bool | None = None) -> bool:
        if critical is None:
            critical = _is_critical_frame(frame)
        accepted = self._try_put(frame, critical)
        if not accepted and critical:
            self._critical_overflow()
        return accepted

    async def _put_on_loop(self, frame: dict, critical: bool) -> None:
        if not critical:
            self.put_nowait(frame, critical=False)
            return
        while not self._try_put(frame, critical=True):
            if self._closed:
                return
            await self._space.wait()

    async def put(self, frame: dict, *, critical: bool | None = None) -> None:
        if critical is None:
            critical = _is_critical_frame(frame)
        if asyncio.get_running_loop() is self.loop:
            await self._put_on_loop(frame, critical)
            return
        future = asyncio.run_coroutine_threadsafe(
            self._put_on_loop(frame, critical), self.loop)
        await asyncio.wrap_future(future)

    async def get(self) -> dict:
        while True:
            if self._items:
                frame, _critical = self._items.popleft()
                if frame is self._gap_frame:
                    self._gap_frame = None
                self._space.set()
                self._flush_gap()
                return frame
            self._wake.clear()
            if self._items:
                continue
            await self._wake.wait()


class _ChatSub:
    """One connection's subscription to a session's chat events. Delivery is
    via loop.call_soon_threadsafe(outbox.put_nowait, …) — correct even if the
    chat task and the connection live on different loops (TestClient runs one
    portal per websocket). skip_upto dedupes against the join-time replay."""

    def __init__(self, loop, outbox: _BoundedOutbox, skip_upto: int):
        self.loop = loop
        self.outbox = outbox
        self.skip_upto = skip_upto


class _SessionChat:
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self.seq = 0
        self.buffer: deque[tuple[int, dict]] = deque(maxlen=_CHAT_BUF_MAX)
        self.subs: set[_ChatSub] = set()
        # The running chat task for this session (any connection bound to the
        # session may cancel it — e.g. the stop button after a page refresh,
        # where the new connection has no per-connection handle).
        self.chat_task: asyncio.Task | None = None
        self.run_id: str | None = None
        self.run_seq = 0
        self.run_snapshot: dict | None = None
        # asyncio locks are loop-bound; this tiny admission lock protects the
        # task handle itself because TestClient may run each websocket on a
        # different event-loop thread.
        self.admission_lock = threading.Lock()

    def active_task(self) -> asyncio.Task | None:
        with self.admission_lock:
            task = self.chat_task
            return task if task is not None and not task.done() else None

    def emit(self, event: dict) -> None:
        """Append a sequenced event, deduplicating replay by run sequence."""
        if not isinstance(event, dict):
            return
        self.run_seq += 1
        event = dict(event)
        # Wall-clock stamp for discreet per-round timestamps in the console.
        # Buffer + replay keep it, so a mid-run rejoin sees real times.
        event.setdefault("ts", time.time())
        # Persist only concise structured events as durable workbench state.
        try:
            agent_core.record_public_timeline_event(self.session_id, event)
        except Exception:
            logger.exception("[%s] failed to persist public timeline event", self.session_id)
        self.seq += 1
        self.buffer.append((self.seq, event))
        for sub in list(self.subs):
            if self.seq > sub.skip_upto:
                sub.loop.call_soon_threadsafe(sub.outbox.put_nowait, event)


    def update_run(self, *, state: str, phase: str | None = None,
                   reason: str | None = None, approval_pause: bool = False,
                   attempt: int | None = None, jobs_list: list | None = None) -> dict:
        if self.run_id is None:
            self.run_id = uuid.uuid4().hex
        if attempt is not None:
            self.run_snapshot = self.run_snapshot or {}
            self.run_snapshot["attempt"] = attempt
        snap = dict(self.run_snapshot or {})
        snap.update({"session_id": self.session_id, "run_id": self.run_id,
                     "seq": self.run_seq, "state": state,
                     "phase": phase or snap.get("phase") or "run",
                     "approval_pause": approval_pause})
        if reason is not None: snap["terminal_reason"] = reason
        if jobs_list is not None: snap["jobs"] = list(jobs_list)
        self.run_snapshot = snap
        # Persist immediately, but do not reorder the established chat frame
        # protocol by injecting a synthetic frame before the first model/tool
        # event. Reconnect obtains this snapshot through project_state.
        _persist_run_snapshot(snap)
        return snap

    def sync_authoritative(self, snapshot: dict | None) -> dict | None:
        """Adopt agent_core's terminal lifecycle without inventing one."""
        if not isinstance(snapshot, dict):
            return self.run_snapshot
        incoming_seq = int(snapshot.get("seq") or 0)
        current_seq = int((self.run_snapshot or {}).get("seq") or 0)
        same_run = (self.run_snapshot is not None and snapshot.get("run_id")
                    == self.run_snapshot.get("run_id"))
        if ((same_run and incoming_seq < current_seq)
                or (not same_run and current_seq > 0)):
            return self.run_snapshot
        if same_run and incoming_seq == current_seq:
            return self.run_snapshot
        self.run_id = snapshot.get("run_id") or self.run_id
        self.run_seq = max(self.run_seq, incoming_seq)
        self.run_snapshot = dict(snapshot)
        self.run_snapshot["session_id"] = self.session_id
        _persist_run_snapshot(self.run_snapshot)
        return self.run_snapshot

    def load_snapshot(self) -> dict | None:
        snapshot = _load_run_snapshot(self.session_id) if self.session_id else None
        if snapshot:
            incoming_seq = int(snapshot.get("seq") or 0)
            current_seq = int((self.run_snapshot or {}).get("seq") or self.run_seq)
            # A delayed disk/REST snapshot must never roll an active session
            # backwards. Equal sequence is idempotent and may fill missing data.
            if self.run_snapshot is None or incoming_seq >= current_seq:
                self.run_id = snapshot.get("run_id")
                self.run_seq = max(self.run_seq, incoming_seq)
                self.run_snapshot = snapshot
        return self.run_snapshot


_SESSIONS: dict[str, _SessionChat] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the jobs DB and reconcile stale jobs from a previous process.
    await jobs.manager.start()
    # A process restart cannot resume paid generation. Mark only persisted
    # running states interrupted; approval pauses are intentionally recoverable.
    try:
        for project in agent_core.list_projects():
            sid = project.get("id") if isinstance(project, dict) else None
            if not isinstance(sid, str):
                continue
            snap = _load_run_snapshot(sid)
            if snap and snap.get("state") == "running":
                snap.update({"session_id": sid, "state": "interrupted",
                             "terminal_reason": "backend restarted while run was active"})
                _persist_run_snapshot(snap)
    except Exception:
        logger.exception("run restart reconciliation failed")
    yield
    await jobs.manager.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(evidence_router)
app.include_router(verification_router)
app.include_router(tapeout_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local dev only
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _history_frames(session_id: str) -> list[dict]:
    """Convert a stored chat history into display messages for the frontend."""
    messages = []
    for entry in agent_core._get_history(session_id):
        role = entry.get("role")
        if role == "user":
            messages.append({"role": "user", "content": entry.get("content", "")})
        elif role == "assistant":
            if entry.get("content"):
                messages.append({"role": "agent", "content": entry["content"]})
            for call in entry.get("tool_calls") or []:
                name = call.get("function", {}).get("name")
                if name:
                    messages.append({"role": "tool_call", "content": name})
        elif role == "tool":
            messages.append({"role": "tool_result", "content": entry.get("content", "")})
    return messages


class _OutboxWS:
    """WebSocket stand-in handed to the JobManager: frames are funneled
    through the connection's outbox queue, so exactly one task performs real
    websocket sends per connection (no concurrent send_json)."""

    def __init__(self, outbox: _BoundedOutbox):
        self.outbox = outbox

    async def send_json(self, frame: dict) -> None:
        if isinstance(frame, dict) and frame.get("type") == "job_started":
            _link_run_job(frame.get("session"), frame.get("job_id"))
        await self.outbox.put(frame)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = uuid.uuid4().hex
    client = websocket.client
    logger.info("[%s] websocket connected from %s", session_id, client)
    loop = asyncio.get_running_loop()

    async def close_for_outbox_overflow() -> None:
        try:
            await websocket.close(code=1013, reason="client too slow")
        except Exception:
            # The receive loop may already have observed the disconnect.
            pass

    def on_critical_outbox_overflow() -> None:
        # Called on the connection loop by _BoundedOutbox. A 1013 close is an
        # explicit, observable policy when a client cannot accept controls.
        asyncio.create_task(close_for_outbox_overflow())

    outbox = _BoundedOutbox(loop, on_critical_overflow=on_critical_outbox_overflow)
    outbox_ws = _OutboxWS(outbox)
    jobs.manager.add_listener(outbox_ws)  # receives job_started broadcasts
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def sender() -> None:
        while True:
            frame = await outbox.get()
            await websocket.send_json(frame)

    # This connection's chat-event subscription (rebound on project join).
    chat_sub: list = [None]  # [(sess, sub)]; list for nonlocal-free mutation

    def bind_chat(sess: _SessionChat) -> None:
        """Detach the old subscription and attach a new one. skip_upto is the
        session's current seq: anything older is replayed inline by the join
        path, so emit() must not re-deliver it."""
        if chat_sub[0] is not None:
            chat_sub[0][0].subs.discard(chat_sub[0][1])
        sub = _ChatSub(asyncio.get_running_loop(), outbox, sess.seq)
        chat_sub[0] = (sess, sub)
        sess.subs.add(sub)

    # Every connection starts bound to its own uuid session.
    bind_chat(_SESSIONS.setdefault(session_id, _SessionChat(session_id)))

    # Chat tasks spawned by this connection (tracked so the disconnect path
    # can spare them; the authoritative handle lives on _SessionChat).
    my_chats: set[asyncio.Task] = set()

    def start_chat(text: str, chat_session: str, *, resume_contract: bool = False) -> asyncio.Task | None:
        sess = _SESSIONS.setdefault(chat_session, _SessionChat(chat_session))
        # Admission and task-handle publication are one cross-loop critical
        # section. In particular, a rejected request never gets a task handle
        # that could overwrite the detached/original run's cancellation handle.
        with sess.admission_lock:
            existing = sess.chat_task
            if existing is not None and not existing.done():
                logger.info("[%s] rejected: a chat is already running for this session",
                            chat_session)
                outbox.put_nowait({"type": "error", "content":
                                   "Agent is already working on this project — wait for the "
                                   "current run to finish (or cancel it)."}, critical=True)
                outbox.put_nowait({"type": "end"}, critical=True)
                return None
            # A new run gets a stable id and durable running state before the
            # first provider call. Existing awaiting-approval state is only
            # replaced by the explicit approved continuation.
            sess.run_id = uuid.uuid4().hex
            sess.run_seq = 0
            sess.run_snapshot = {"session_id": chat_session, "run_id": sess.run_id}
            sess.update_run(state="running", phase="generation",
                            approval_pause=False)
            task = spawn(run_chat(text, chat_session, resume_contract=resume_contract))
            sess.chat_task = task
            my_chats.add(task)

        def clear_chat_task(done: asyncio.Task) -> None:
            my_chats.discard(done)
            with sess.admission_lock:
                if sess.chat_task is done:
                    sess.chat_task = None

        task.add_done_callback(clear_chat_task)
        return task

    async def run_chat(text: str, chat_session: str, *, resume_contract: bool = False) -> None:
        # Admission is performed synchronously by start_chat before this task
        # is scheduled. The asyncio lock remains a defensive serialization
        # guard around the actual agent loop.
        sess = _SESSIONS.setdefault(chat_session, _SessionChat(chat_session))
        async with sess.lock:
            # Fresh run: drop the previous run's buffered events, then publish
            # every chat event to the SESSION buffer (not this connection's
            # outbox) so any client bound to the session — including one that
            # rejoins mid-run — sees them.
            sess.buffer.clear()
            sess.seq = 0
            sess.update_run(state="running", phase="generation")
            try:
                chat_kwargs = {"resume_contract": True} if resume_contract else {}
                async for event in agent_core.chat(
                        text, session_id=chat_session, **chat_kwargs):
                    sess.emit(event)
                    if event.get("type") in {
                            "progress", "decision", "phase", "timeline",
                            "run_progress", "run_status", "structured_progress",
                            "structured_status", "decision_status"}:
                        snapshot = _live_run_state(chat_session)
                        if snapshot is not None:
                            sess.emit({"type": "run_state", **snapshot})
                # agent_core owns lifecycle classification (including
                # awaiting approval, inconclusive, and terminal reasons).
                sess.sync_authoritative(_load_run_snapshot(chat_session))
                if sess.run_snapshot is not None:
                    sess.emit({"type": "run_state", **sess.run_snapshot})
                sess.emit({"type": "end"})
            except asyncio.CancelledError:
                logger.info("[%s] chat cancelled by user", chat_session)
                try:
                    cancelled = await jobs.manager.cancel_session(chat_session)
                    logger.info("[%s] cancelled %d owned job(s)",
                                chat_session, cancelled)
                except Exception:
                    logger.exception("[%s] failed to cancel owned jobs", chat_session)
                sess.sync_authoritative(_load_run_snapshot(chat_session))
                if sess.run_snapshot is not None:
                    sess.emit({"type": "run_state", **sess.run_snapshot})
                sess.emit({"type": "error", "content": "Run cancelled by user."})
                sess.emit({"type": "end"})
                raise
            except Exception as e:
                logger.exception("[%s] error while handling message", chat_session)
                sess.sync_authoritative(_load_run_snapshot(chat_session))
                if sess.run_snapshot is not None:
                    sess.emit({"type": "run_state", **sess.run_snapshot})
                sess.emit({"type": "error", "content": str(e)})
                sess.emit({"type": "end"})

    sender_task = asyncio.create_task(sender())
    try:
        while True:
            data = await websocket.receive_text()

            try:
                frame = json.loads(data)
            except json.JSONDecodeError:
                frame = None

            # Typed Design Contract actions are handled before project joins
            # because they also carry a project field.  They are pure state
            # transitions: no agent/EDA call is reachable from this branch.
            if isinstance(frame, dict) and frame.get("type") in {
                    "contract_action", "genui_action", "action"} or (
                    isinstance(frame, dict) and isinstance(frame.get("action"), str)
                    and frame.get("action", "").startswith("contract.")):
                action = frame.get("action")
                action_project = frame.get("project") or session_id
                payload = frame.get("payload", frame.get("data", {}))
                if not isinstance(payload, dict):
                    payload = {}
                expected_revision = frame.get("expected_revision", frame.get("revision", payload.pop("expected_revision", payload.pop("revision", None))))
                expected_digest = frame.get("expected_digest", frame.get("digest", payload.pop("expected_digest", payload.pop("digest", None))))
                if action_project != session_id:
                    outbox.put_nowait({"type": "contract_error", "code": "project_mismatch",
                                       "error": "contract action project does not match the bound session"})
                    continue
                try:
                    result = agent_core.apply_design_contract_action(
                        session_id, project_id=action_project, action=action,
                        expected_revision=expected_revision, expected_digest=expected_digest,
                        payload=payload)
                except ContractProtocolError as exc:
                    error = {"type": "contract_error", "code": exc.code,
                             "error": str(exc), "project": action_project}
                    authoritative = agent_core.get_design_contract(session_id)
                    if authoritative:
                        error["authoritative_state"] = authoritative
                    outbox.put_nowait(error)
                except Exception as exc:
                    logger.exception("[%s] contract action failed", session_id)
                    outbox.put_nowait({"type": "contract_error", "code": "invalid_contract",
                                       "error": str(exc), "project": action_project})
                else:
                    # Fan out the backend-authoritative complete component set
                    # to every client watching this project.
                    sess = _SESSIONS.setdefault(session_id, _SessionChat(session_id))
                    sess.emit({"type": "contract_state", **result})
                    # Approve & build consumes the retained original request.
                    # The typed action itself never invokes EDA; it only admits
                    # one serialized continuation through the normal chat loop.
                    if (action in {"contract.approve", "contract.approve_and_build"}
                            and result.get("status") == "locked"):
                        pending = agent_core.get_pending_contract_request(session_id)
                        if pending:
                            start_chat(pending, session_id, resume_contract=True)
                continue

            # Project join frame: {"project": "proj-..."} — switches this
            # connection's session and replays the persisted history.
            if isinstance(frame, dict) and isinstance(frame.get("project"), str):
                session_id = frame["project"]
                agent_core._get_history(session_id)  # loads from disk if persisted
                logger.info("[%s] joined project, %d history entries",
                            session_id, len(agent_core._get_history(session_id)))
                await outbox.put({"type": "history", "content": _history_frames(session_id)})
                # Restore the workbench (Verilog viewer + metric cards) from
                # the persisted project state; keys are omitted when absent,
                # and old clients simply ignore the unknown frame type.
                state = agent_core.get_project_state(session_id)
                existing_session = _SESSIONS.get(session_id)
                active_before_bind = bool(existing_session and existing_session.active_task())
                if state and not active_before_bind:
                    await outbox.put({"type": "project_state", **state})
                contract_state = agent_core.get_design_contract(session_id)
                if contract_state:
                    await outbox.put({"type": "contract_state", **contract_state})
                # Rebind the chat-event subscription to this session. If a
                # chat is mid-run (lock held — e.g. a detached run started
                # before a page refresh), say so and replay the run's buffered
                # events; events emitted after the bind are delivered live by
                # emit() and deduped via the subscription's skip_upto.
                sess = _SESSIONS.setdefault(session_id, _SessionChat(session_id))
                persisted = sess.load_snapshot()
                running = sess.active_task() is not None
                # No task survives a restart. Surface stale running state as
                # interrupted and never start generation from a reconnect.
                if persisted and persisted.get("state") == "running" and not running:
                    persisted.update({"session_id": session_id, "state": "interrupted",
                                      "terminal_reason": "backend restarted while run was active"})
                    sess.run_snapshot = persisted
                    _persist_run_snapshot(persisted)
                bind_chat(sess)
                sub = chat_sub[0][1]
                if running:
                    await outbox.put({"type": "busy"})
                    for seq, event in list(sess.buffer):
                        if seq <= sub.skip_upto:
                            await outbox.put(event)
                continue

            # Job subscription: {"type": "subscribe", "job_id": ..., "since_seq": N}
            # — replays ring entries with seq > since_seq, then streams live.
            # Handled while a chat is in flight: the receive loop never blocks
            # on chat, so a client can attach to a running job's log live.
            if isinstance(frame, dict) and frame.get("type") == "subscribe":
                job_id = frame.get("job_id")
                if isinstance(job_id, str):
                    try:
                        since_seq = int(frame.get("since_seq") or 0)
                    except (TypeError, ValueError):
                        since_seq = 0
                    spawn(jobs.manager.subscribe(job_id, outbox_ws, since_seq))
                    logger.info("[%s] subscribed to job %s (since_seq=%d)",
                                session_id, job_id, since_seq)
                continue

            # Job cancellation: {"type": "cancel_job", "job_id": ...}
            if isinstance(frame, dict) and frame.get("type") == "cancel_job":
                job_id = frame.get("job_id")
                if isinstance(job_id, str):
                    logger.info("[%s] cancel requested for job %s", session_id, job_id)
                    await jobs.manager.cancel(job_id)
                continue

            # Chat cancellation: {"type": "cancel"} — cancels the chat task of
            # the session this connection is CURRENTLY BOUND to (tracked on
            # _SessionChat, not per connection), so the stop button still works
            # after a page refresh and from any client watching the project.
            if isinstance(frame, dict) and frame.get("type") == "cancel":
                sess = chat_sub[0][0] if chat_sub[0] is not None else None
                chat_task = sess.active_task() if sess is not None else None
                if chat_task is not None and not chat_task.done():
                    logger.info("[%s] cancel requested for session chat", session_id)
                    # The task may live on another connection's loop; cancel
                    # via its own loop (thread-safe, and correct single-loop).
                    chat_task.get_loop().call_soon_threadsafe(chat_task.cancel)
                else:
                    logger.debug("[%s] cancel frame ignored (no chat running)", session_id)
                continue

            start_chat(data, session_id)
    except WebSocketDisconnect:
        if not session_id.startswith("proj-"):
            agent_core.reset_session(session_id)
            # Ephemeral uuid sessions die with their connection.
            sess = _SESSIONS.get(session_id)
            if sess is not None and sess.active_task() is None:
                _SESSIONS.pop(session_id, None)
        logger.info("[%s] websocket disconnected", session_id)
    finally:
        sender_task.cancel()
        jobs.manager.remove_listener(outbox_ws)
        # Unbind the chat subscription; a mid-run chat keeps emitting into the
        # session buffer (detached), ready for the next join to replay.
        if chat_sub[0] is not None:
            chat_sub[0][0].subs.discard(chat_sub[0][1])
        for task in list(tasks):
            # Do NOT cancel an in-flight chat: the agent run keeps going
            # server-side (its history persists when it finishes), so a
            # browser refresh no longer kills a synthesis mid-round.
            # With no subscribers, events remain in the bounded session buffer
            # for the next project join; a slow live subscriber gets an
            # explicit outbox_gap marker instead of unbounded queue growth.
            if task in my_chats and not task.done():
                logger.info("[%s] client gone; letting chat task finish in background",
                            session_id)
                _DETACHED_CHATS.add(task)  # keep a strong ref (see above)
                task.add_done_callback(_DETACHED_CHATS.discard)
                continue
            task.cancel()


class ProjectCreate(BaseModel):
    name: str = "Untitled project"
    model: str | None = None  # optional registry key (see GET /api/models)
    reasoning_effort: str | None = None  # effort for models that declare levels


@app.get("/api/projects")
def list_projects():
    return agent_core.list_projects()


@app.get("/api/models")
async def list_models():
    """Model registry entries for the frontend dropdown, with a cached health
    status per entry (free auth-check probes; fast path served from the TTL
    cache, worst case one ~5 s probe round on cache miss)."""
    models = model_registry.list_models()
    health = await model_registry.verify_all()
    for m in models:
        m["health"] = health[m["key"]]
    return models


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate):
    try:
        return agent_core.create_project(body.name, model=body.model,
                                         reasoning_effort=body.reasoning_effort)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class ProjectPatch(BaseModel):
    name: str | None = None
    model: str | None = None  # optional registry key (see GET /api/models)
    reasoning_effort: str | None = None  # effort for models that declare levels


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectPatch):
    name = None
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Project name must not be empty")
        if len(name) > 120:
            raise HTTPException(status_code=422, detail="Project name must be at most 120 characters")
    # Distinguish "field absent" (leave unchanged) from null (clear back to
    # the registry default).
    effort = (body.reasoning_effort
              if "reasoning_effort" in body.model_fields_set
              else agent_core.UNSET)
    try:
        project = agent_core.update_project(project_id, name=name, model=body.model,
                                            reasoning_effort=effort)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.get("/api/projects/{project_id}/hardware-project")
def get_hardware_project(project_id: str):
    try:
        project = agent_core.get_hardware_project(project_id)
    except HardwareProjectError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=404, detail="Hardware project not found")
    return project


@app.put("/api/projects/{project_id}/hardware-project")
def replace_hardware_project(project_id: str, body: HardwareProjectUpdate):
    try:
        project = agent_core.replace_hardware_project(project_id, body)
    except HardwareProjectError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    if not agent_core.delete_project(project_id):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Project not found")


@app.get("/api/jobs")
async def list_jobs():
    """Build registry: recent EDA jobs, newest first."""
    return await jobs.manager.list_jobs()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Detailed execution and evidence metadata for a selected job."""
    detail = await jobs.manager.get_job(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return detail


async def _load_job_gate_netlist(job_id: str):
    """Resolve a job's durable gate-netlist artifact (or honest absence).

    The live manifest is authoritative for a running backend; the persisted
    ``netlist_sha256`` column keeps the identity across restarts.
    """
    detail = await jobs.manager.get_job(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Job not found")
    manifest = detail.get("manifest") if isinstance(detail.get("manifest"), dict) else {}
    meta = manifest.get("gate_netlist") if isinstance(manifest.get("gate_netlist"), dict) else {}
    digest = meta.get("repository_sha256") or detail.get("netlist_sha256")
    if not isinstance(digest, str) or not digest:
        return None, gate_netlist.unavailable(
            (meta or {}).get("reason")
            or "this job produced no structured gate netlist artifact")
    import evidence
    content = evidence.repository.get_artifact(digest)
    if content is None:
        return None, gate_netlist.unavailable("stored gate netlist artifact is missing")
    try:
        artifact = gate_netlist.load_gate_netlist(json.loads(content))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, gate_netlist.unavailable(f"stored gate netlist artifact rejected: {exc}")
    return (artifact, digest), None


@app.get("/api/jobs/{job_id}/netlist")
async def job_netlist(job_id: str):
    """Gate-netlist artifact metadata for a synthesis job (Phase G1)."""
    loaded, absent = await _load_job_gate_netlist(job_id)
    if absent is not None:
        return absent
    artifact, digest = loaded
    return gate_netlist.artifact_summary(artifact, repository_sha256=digest,
                                         size_bytes=len(gate_netlist.canonical_bytes(artifact)))


@app.get("/api/jobs/{job_id}/netlist/graph")
async def job_netlist_graph(job_id: str, module: str | None = None,
                            depth: int = 1, limit: int = 500,
                            cone: str | None = None, cone_hops: int = 6,
                            artifact_sha256: str | None = None):
    """Bounded hierarchical gate-graph view of a job's netlist artifact.

    ``artifact_sha256`` pins the request to one artifact: a mismatch is a
    stale request and is rejected instead of silently combining evidence.
    """
    loaded, absent = await _load_job_gate_netlist(job_id)
    if absent is not None:
        return absent
    artifact, digest = loaded
    if artifact_sha256 is not None:
        expected = artifact.get("artifact_sha256")
        if artifact_sha256 not in (expected, digest):
            raise HTTPException(status_code=409,
                                detail="artifact digest mismatch: the request refers to a stale or different netlist")
    try:
        return gate_netlist.bounded_graph(artifact, module=module, depth=depth,
                                          node_limit=limit, cone=cone,
                                          cone_hops=cone_hops)
    except gate_netlist.GateNetlistError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _load_job_timing_paths(job_id: str):
    """Resolve a job's durable timing-path artifact (or honest absence)."""
    detail = await jobs.manager.get_job(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Job not found")
    manifest = detail.get("manifest") if isinstance(detail.get("manifest"), dict) else {}
    meta = manifest.get("timing_paths") if isinstance(manifest.get("timing_paths"), dict) else {}
    digest = meta.get("repository_sha256") or detail.get("timing_paths_sha256")
    if not isinstance(digest, str) or not digest:
        return None, timing_paths.timing_paths_unavailable(
            (meta or {}).get("reason")
            or "this job produced no ordered timing-path artifact")
    import evidence
    content = evidence.repository.get_artifact(digest)
    if content is None:
        return None, timing_paths.timing_paths_unavailable("stored timing-path artifact is missing")
    try:
        artifact = timing_paths.load_timing_paths(json.loads(content))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, timing_paths.timing_paths_unavailable(f"stored timing-path artifact rejected: {exc}")
    return (artifact, digest), None


@app.get("/api/jobs/{job_id}/timing-paths")
async def job_timing_paths(job_id: str, netlist_sha256: str | None = None):
    """Ordered timing paths mapped onto the job's gate netlist (Phase G3).

    ``netlist_sha256`` pins the request to one netlist: a mismatch is
    rejected, never silently combined with a different netlist's paths.
    """
    loaded, absent = await _load_job_timing_paths(job_id)
    if absent is not None:
        return absent
    artifact, digest = loaded
    identity = artifact.get("identity") or {}
    if netlist_sha256 is not None and netlist_sha256 != identity.get("netlist_sha256"):
        raise HTTPException(status_code=409,
                            detail="netlist digest mismatch: these timing paths belong to a different netlist")
    body = dict(artifact)
    body["identity"] = {**identity, "repository_sha256": digest}
    return body


@app.get("/api/jobs/{job_id}/layout")
async def job_layout_view(job_id: str, netlist_sha256: str | None = None):
    """Indexed physical layout view from the job's routed DEF (Phase G4).

    Only a post-P&R run has a DEF; a screening synthesis returns an honest
    ``unavailable`` record. ``netlist_sha256`` pins the request to one
    netlist so a stale view can never be combined with another netlist's
    selection.
    """
    detail = await jobs.manager.get_job(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Job not found")
    manifest = detail.get("manifest") if isinstance(detail.get("manifest"), dict) else {}
    meta = manifest.get("layout_view") if isinstance(manifest.get("layout_view"), dict) else {}
    digest = meta.get("repository_sha256") or detail.get("layout_view_sha256")
    if not isinstance(digest, str) or not digest:
        return physical_layout.layout_view_unavailable(
            (meta or {}).get("reason")
            or "this job produced no physical implementation (no DEF)")
    import evidence
    content = evidence.repository.get_artifact(digest)
    if content is None:
        return physical_layout.layout_view_unavailable("stored layout view artifact is missing")
    try:
        import json as _json
        artifact = physical_layout.load_layout_view(_json.loads(content))
    except (ValueError, TypeError, _json.JSONDecodeError) as exc:
        return physical_layout.layout_view_unavailable(f"stored layout view rejected: {exc}")
    identity = artifact.get("identity") or {}
    if netlist_sha256 is not None and netlist_sha256 != identity.get("netlist_sha256"):
        raise HTTPException(status_code=409,
                            detail="netlist digest mismatch: this layout belongs to a different netlist")
    body = dict(artifact)
    body["identity"] = {**identity, "repository_sha256": digest}
    return body


# -- Mistral setup wizard (Phase: provider setup, mirrors `vibe --setup`) ------
# The API key never travels back to a client: the browser flow exchanges it
# server-side, verifies it with the FREE /v1/models probe, and writes it into
# the same .env the backend loads.

@app.get("/api/providers/mistral/setup/state")
async def mistral_setup_state():
    """Key-set flag and in-flight browser-flow status (no credentials)."""
    return mistral_setup.setup_state()


@app.post("/api/providers/mistral/setup/browser/start")
async def mistral_setup_browser_start(body: dict | None = None):
    """Start the PKCE browser sign-in (optionally on a custom login domain)."""
    console_base_url = None
    if isinstance(body, dict):
        raw = body.get("console_base_url")
        if raw is not None and (not isinstance(raw, str) or not raw.strip() or len(raw) > 200):
            raise HTTPException(status_code=422, detail="console_base_url must be a short non-empty string")
        console_base_url = raw.strip() or None if isinstance(raw, str) else None
    try:
        return await mistral_setup.start_browser_sign_in(console_base_url=console_base_url)
    except mistral_setup.SetupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/providers/mistral/setup/browser/status")
async def mistral_setup_browser_status():
    """Poll the in-flight sign-in process once (the UI sets the cadence)."""
    try:
        return await mistral_setup.poll_browser_sign_in()
    except mistral_setup.SetupError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/providers/mistral/setup/browser/complete")
async def mistral_setup_browser_complete():
    """Exchange the completed flow, verify the key, and store it in .env."""
    try:
        return await mistral_setup.complete_browser_sign_in()
    except mistral_setup.SetupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/providers/mistral/setup/key")
async def mistral_setup_submit_key(body: dict):
    """Verify a pasted key with the free probe and store it in .env."""
    api_key = body.get("api_key") if isinstance(body, dict) else None
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 512:
        raise HTTPException(status_code=422, detail="api_key must be a non-empty string")
    health = await mistral_setup.verify_api_key(api_key.strip())
    if health != "ok":
        # The key is never echoed back; only the probe verdict.
        return {"health": health, "saved": False}
    try:
        path = mistral_setup.save_api_key_to_env(api_key.strip(), force=True)
    except mistral_setup.SetupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"health": health, "saved": True, "env_path": str(path)}


@app.get("/health")
async def health_check():
    default = model_registry.get_model(model_registry.DEFAULT_MODEL_KEY)
    health = await model_registry.verify_all()
    return {
        "status": "ok",
        "model": default["model_id"],
        "api_key_set": bool(os.environ.get("KIMI_API_KEY")),
        "models_available": sum(1 for m in model_registry.list_models() if m["available"]),
        "models_healthy": sum(1 for h in health.values() if h == "ok"),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
