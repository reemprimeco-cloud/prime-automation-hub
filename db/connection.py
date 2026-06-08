"""Database connection.

All DB access uses SQLite by default (DATABASE_PATH env var).
For production (PostgreSQL), this module is the only file that needs changing —
swap get_connection() to use psycopg2 or asyncpg with the same interface.
"""
from __future__ import annotations

import os
import sqlite3


def get_connection() -> sqlite3.Connection:
    """Return a configured SQLite connection with WAL mode and row factory."""
    db_path = os.getenv("DATABASE_PATH", "hub.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")     # allows concurrent reads + writes
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
