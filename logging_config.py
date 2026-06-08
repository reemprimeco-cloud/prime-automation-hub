"""Structured logging configuration.

All log lines written to logs/app.log are newline-delimited JSON so they can be
parsed by any log aggregator. Console output is plain text at INFO+.

Usage
-----
    from logging_config import get_logger
    _LOG = get_logger("client")
    _LOG.info("api_request", extra={"method": "GET", "path": "/...", "status": 200, "duration_ms": 142})

Setup is lazy — the first call to get_logger() creates the handlers if they don't
exist yet. Subsequent calls are no-ops, so this is safe to call anywhere.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from pathlib import Path

_LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
_LOG_FILE = _LOG_DIR / "app.log"
_LOGGER_NAME = "qbo"

_STANDARD_ATTRS = frozenset(
    {
        "args", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs",
        "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a compact JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        entry: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }
        # Include any extra key=value pairs passed via the `extra` kwarg.
        for key, val in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = val
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger(_LOGGER_NAME)
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)

    # Rotating JSON file handler — 5 MB × 3 backups
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonFormatter())

    # Plain-text console handler (INFO and above)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(
        logging.Formatter("%(levelname)-8s %(name)s – %(message)s")
    )

    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the 'qbo' hierarchy, setting up handlers on first call."""
    setup_logging()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
