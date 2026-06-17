"""SQLite-backed storage for received QuickBooks webhook events.

Each row represents a single entity change (Invoice Create, Customer Update, etc.)
extracted from the notification payload. The full raw JSON is stored on every row
so nothing is lost even if our parsing logic changes later.

DB path is read from the WEBHOOK_DB_PATH environment variable (default: webhook_events.db).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from logging_config import get_logger
from webhook.payload import parse_qbo_webhook_entities, payload_format_hint

_LOG = get_logger("webhook.storage")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS webhook_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT    NOT NULL,
    realm_id     TEXT,
    entity_type  TEXT,
    operation    TEXT,
    entity_id    TEXT,
    raw_payload  TEXT    NOT NULL
)
"""

_INSERT = """
INSERT INTO webhook_events (received_at, realm_id, entity_type, operation, entity_id, raw_payload)
VALUES (?, ?, ?, ?, ?, ?)
"""


def _db_path() -> str:
    return os.getenv("WEBHOOK_DB_PATH", "webhook_events.db")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path())


def init_db() -> None:
    """Create the events table if it doesn't already exist (idempotent)."""
    with _connect() as conn:
        conn.execute(_CREATE_TABLE)
        conn.commit()


def store_webhook_payload(payload: dict, raw_json: str) -> int:
    """Parse a QBO webhook notification and persist each entity change.

    Returns the number of events stored.
    """
    init_db()  # idempotent — safe to call on every request
    received_at = datetime.now(timezone.utc).isoformat()
    rows: list[tuple] = []

    for entity in parse_qbo_webhook_entities(payload):
        rows.append(
            (
                received_at,
                entity["realm_id"],
                entity["entity_type"],
                entity["operation"],
                entity["entity_id"],
                raw_json,
            )
        )

    if rows:
        with _connect() as conn:
            conn.executemany(_INSERT, rows)
            conn.commit()
        _LOG.info(
            "webhook_events_stored",
            extra={"count": len(rows), "realm_id": rows[0][1] if rows else ""},
        )
    else:
        _LOG.warning(
            "webhook_payload_had_no_entity_changes",
            extra={"format": payload_format_hint(payload)},
        )

    return len(rows)


def count_all_events() -> int:
    """Return total number of stored webhook events (0 if the DB doesn't exist)."""
    if not os.path.exists(_db_path()):
        return 0
    try:
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def get_recent_events(limit: int = 20) -> list[dict]:
    """Return the most recently received events as plain dicts."""
    if not os.path.exists(_db_path()):
        return []
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, received_at, realm_id, entity_type, operation, entity_id "
            "FROM webhook_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_webhook_received_at() -> str | None:
    """Return ISO timestamp of the most recent stored webhook, or None."""
    if not os.path.exists(_db_path()):
        return None
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT received_at FROM webhook_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None
