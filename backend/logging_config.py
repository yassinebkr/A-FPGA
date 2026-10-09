"""Central logging setup: rotating file at backend/logs/backend.log + console.

Log level is controlled by LOG_LEVEL (default INFO). Every run, crash,
tool call and LLM round is recorded with a session id so any run can be
reconstructed after the fact.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "backend.log")

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(LOG_DIR, exist_ok=True)
    level = os.environ.get("LOG_LEVEL", "INFO").upper()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # uvicorn's own loggers propagate to root, so access/error logs land in the file too.
    logging.getLogger(__name__).info("Logging initialized at %s (level=%s)", LOG_FILE, level)
