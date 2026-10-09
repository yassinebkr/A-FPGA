"""Tool-result cache (Task 5).

Caches the results of write_and_check_syntax and run_simulation so the agent
never pays twice for identical inputs. Key:
sha256(verilog_code + testbench_code_or_empty + tool_name + tool_version)
— tool_name/tool_version are the underlying EDA binary ("verilator 5.032 …"),
since that is what actually determines the result. Synthesis is deliberately
NOT cached (too stateful: PDK, flow choice, SDC, build dir).

Storage is plain synchronous sqlite3 at backend/data/tool_cache.db (override
with the TOOL_CACHE_DB env var; tests point it at tmp_path), NOT aiosqlite:
the cache is touched from the synchronous write_and_check_syntax fast path as
well as the event loop, operations are single-row get/put on a tiny DB, and a
short-lived connection per call sidesteps the cross-thread/loop connection
state (aiosqlite's non-daemon worker thread) that jobs.py has to manage.
"""
import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("eda")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_cache (
  key TEXT PRIMARY KEY,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "tool_cache.db"


def _db_path() -> str:
    return os.environ.get("TOOL_CACHE_DB", str(_DEFAULT_PATH))


def make_key(tool_name: str, tool_version: str, verilog_code: str,
             testbench_code: str = "") -> str:
    """sha256 over the inputs that determine the tool result, NUL-separated
    so field boundaries can't collide."""
    h = hashlib.sha256()
    for part in (tool_name, tool_version, verilog_code, testbench_code or ""):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()


def get(key: str) -> str | None:
    """Cached result text for *key*, or None."""
    try:
        path = _db_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        db = sqlite3.connect(path)
        try:
            db.execute(_SCHEMA)
            row = db.execute("SELECT result FROM tool_cache WHERE key=?",
                             (key,)).fetchone()
        finally:
            db.close()
    except sqlite3.Error as e:
        logger.warning("tool cache read failed: %s", e)
        return None
    return row[0] if row else None


def put(key: str, result: str) -> None:
    try:
        path = _db_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        db = sqlite3.connect(path)
        try:
            db.execute(_SCHEMA)
            db.execute(
                "INSERT OR REPLACE INTO tool_cache (key, result, created_at)"
                " VALUES (?,?,?)",
                (key, result, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            db.commit()
        finally:
            db.close()
    except sqlite3.Error as e:
        logger.warning("tool cache write failed: %s", e)
