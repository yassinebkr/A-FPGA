"""Durable storage for chat projects.

The project API is intentionally synchronous because it is also used by the
FastAPI REST handlers and by the websocket join path.  This module keeps the
blocking section small (JSON serialization and one atomic replace) and makes
the important durability/concurrency guarantees in one place:

* each project has its own re-entrant lock, so a chat save cannot race a REST
  rename/model update or delete;
* writes go to a temporary file in the project directory, are flushed and
  fsynced, then atomically installed with ``os.replace``;
* a failed serialization/write leaves the previous project file untouched.

The repository does not hold a lock while callers perform model or EDA work.
Callers use :meth:`lock` around their in-memory state mutation and snapshot,
so this small synchronous file operation does not serialize long-running work.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class CorruptProjectError(ValueError):
    """Raised when a project file is not a valid persisted project."""


class ProjectRepository:
    """Atomic, per-project JSON persistence.

    ``directory`` is resolved at construction time.  The agent creates a new
    instance when its test/runtime ``PROJECTS_DIR`` setting changes, which
    keeps monkeypatched test directories isolated as well.
    """

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory).absolute()
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    @staticmethod
    def _valid_id(project_id: str) -> bool:
        return (
            isinstance(project_id, str)
            and project_id.startswith("proj-")
            and "/" not in project_id
            and "\\" not in project_id
            and project_id not in {"proj-", "proj-.", "proj-.."}
        )

    def path(self, project_id: str) -> Path | None:
        if not self._valid_id(project_id):
            return None
        return self.directory / f"{project_id}.json"

    def lock_for(self, project_id: str) -> threading.RLock:
        """Return a stable lock for one project id."""
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.RLock())

    @contextmanager
    def lock(self, project_id: str) -> Iterator[None]:
        """Serialize all operations involving one project's state."""
        lock = self.lock_for(project_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def exists(self, project_id: str) -> bool:
        path = self.path(project_id)
        return bool(path and path.exists())

    def load(self, project_id: str) -> dict | None:
        """Load one project, returning ``None`` when it does not exist."""
        path = self.path(project_id)
        if path is None or not path.exists():
            return None
        with self.lock(project_id):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (json.JSONDecodeError, OSError) as exc:
                raise CorruptProjectError(str(path)) from exc
            if not isinstance(data, dict) or not isinstance(data.get("history"), list):
                raise CorruptProjectError(str(path))
            return data

    def save(self, project_id: str, data: dict) -> None:
        """Atomically persist a project snapshot.

        The temporary file is created in the destination directory, ensuring
        that ``os.replace`` remains atomic even when the project directory is
        on a separate filesystem from the process' default temp directory.
        """
        path = self.path(project_id)
        if path is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock(project_id):
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.directory,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary_name = handle.name
                    json.dump(data, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path)
                temporary_name = None
                # Persist the directory entry too.  Some filesystems do not
                # need this for normal operation, but it closes the durability
                # gap between replace and a sudden host/power failure.
                try:
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                except OSError:
                    directory_fd = None
                if directory_fd is not None:
                    try:
                        try:
                            os.fsync(directory_fd)
                        except OSError:
                            # The file replacement is already atomic; a
                            # filesystem that cannot fsync directories still
                            # gets the correct visible snapshot.
                            pass
                    finally:
                        os.close(directory_fd)
            finally:
                if temporary_name:
                    try:
                        os.unlink(temporary_name)
                    except FileNotFoundError:
                        pass

    def delete(self, project_id: str) -> bool:
        path = self.path(project_id)
        if path is None:
            return False
        with self.lock(project_id):
            try:
                path.unlink()
            except FileNotFoundError:
                return False
            return True

    def iter_projects(self) -> Iterator[tuple[str, dict, Path, float]]:
        """Yield valid project records and skip missing/corrupt files."""
        if not self.directory.is_dir():
            return
        try:
            paths = sorted(self.directory.glob("proj-*.json"))
        except OSError:
            return
        for path in paths:
            project_id = path.stem
            try:
                data = self.load(project_id)
                if data is None:
                    continue
                mtime = path.stat().st_mtime
            except (CorruptProjectError, OSError):
                continue
            yield project_id, data, path, mtime

