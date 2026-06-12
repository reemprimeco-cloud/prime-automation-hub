"""Data access layer for the payment_links table.

All functions use only standard Python and sqlite3 — no ORM, no extra deps.
Column order matches the migration SQL exactly.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from db.connection import get_connection
from logging_config import get_logger

_LOG = get_logger("db.payment_links")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS payment_links (
    id               TEXT NOT NULL PRIMARY KEY,
    invoice_id       TEXT NOT NULL UNIQUE,
    customer_id      TEXT NOT NULL,
    invoice_number   TEXT,
    customer_name    TEXT,
    tap_charge_id    TEXT UNIQUE,
    payment_url      TEXT,
    amount           REAL NOT NULL,
    currency         TEXT NOT NULL DEFAULT 'KWD',
    status           TEXT NOT NULL DEFAULT 'LINK_GENERATED',
    qbo_note_updated INTEGER NOT NULL DEFAULT 0,
    qbo_note_updated_at TEXT,
    whatsapp_sent        INTEGER NOT NULL DEFAULT 0,
    whatsapp_message_sid TEXT,
    whatsapp_to_number   TEXT,
    whatsapp_sent_at     TEXT,
    whatsapp_error       TEXT,
    qbo_payment_id       TEXT,
    payment_captured_at  TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
"""
_CREATE_IDX_INVOICE  = "CREATE UNIQUE INDEX IF NOT EXISTS idx_pl_invoice_id  ON payment_links (invoice_id)"
_CREATE_IDX_CHARGE   = "CREATE        INDEX IF NOT EXISTS idx_pl_charge_id   ON payment_links (tap_charge_id)"
_CREATE_IDX_STATUS   = "CREATE        INDEX IF NOT EXISTS idx_pl_status      ON payment_links (status)"


# ── schema initialisation ─────────────────────────────────────────────────────

def init_table() -> None:
    """Create the table and indexes if they do not exist (idempotent)."""
    with get_connection() as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_IDX_INVOICE)
        conn.execute(_CREATE_IDX_CHARGE)
        conn.execute(_CREATE_IDX_STATUS)
        conn.commit()
    _LOG.debug("payment_links_table_ready")


# ── writes ────────────────────────────────────────────────────────────────────

def create_link(
    *,
    invoice_id: str,
    customer_id: str,
    tap_charge_id: str,
    payment_url: str,
    amount: float,
    currency: str = "KWD",
    invoice_number: str = "",
    customer_name: str = "",
) -> dict:
    """Insert a new payment link record and return it as a dict."""
    now = _utcnow()
    row_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO payment_links
                (id, invoice_id, customer_id, invoice_number, customer_name,
                 tap_charge_id, payment_url, amount, currency,
                 status, qbo_note_updated, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'LINK_GENERATED',0,?,?)
            """,
            (row_id, invoice_id, customer_id, invoice_number, customer_name,
             tap_charge_id, payment_url, amount, currency, now, now),
        )
        conn.commit()
    _LOG.info(
        "payment_link_created",
        extra={"invoice_id": invoice_id, "tap_charge_id": tap_charge_id},
    )
    return get_by_invoice_id(invoice_id)   # type: ignore[return-value]


def register_existing_link(
    *,
    invoice_id: str,
    customer_id: str,
    tap_charge_id: str,
    payment_url: str,
    amount: float,
    currency: str = "KWD",
    invoice_number: str = "",
    customer_name: str = "",
    whatsapp_to_number: str = "",
    qbo_note_updated: bool = True,
    whatsapp_sent: bool = True,
) -> bool:
    """Register a payment link created outside this hub (idempotent).

    Used to sync links generated locally so Render can auto-capture Tap webhooks.
    Returns True if a new row was inserted, False if already registered.
    """
    init_table()
    if get_by_invoice_id(invoice_id) or get_by_charge_id(tap_charge_id):
        return False

    now = _utcnow()
    row_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO payment_links
                (id, invoice_id, customer_id, invoice_number, customer_name,
                 tap_charge_id, payment_url, amount, currency, status,
                 qbo_note_updated, qbo_note_updated_at,
                 whatsapp_sent, whatsapp_to_number, whatsapp_sent_at,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'LINK_GENERATED',?,?,?,?,?,?,?)
            """,
            (
                row_id, invoice_id, customer_id, invoice_number, customer_name,
                tap_charge_id, payment_url, amount, currency,
                1 if qbo_note_updated else 0,
                now if qbo_note_updated else None,
                1 if whatsapp_sent else 0,
                whatsapp_to_number or None,
                now if whatsapp_sent and whatsapp_to_number else None,
                now, now,
            ),
        )
        conn.commit()
    _LOG.info(
        "payment_link_registered",
        extra={"invoice_id": invoice_id, "tap_charge_id": tap_charge_id},
    )
    return True


def bootstrap_links_from_file(path: str | None = None) -> int:
    """Import missing payment links from a JSON file (runs on server startup).

    Each entry needs: invoice_id, customer_id, tap_charge_id, payment_url, amount.
    Optional: invoice_number, customer_name, currency, whatsapp_to_number.
    """
    import json
    from pathlib import Path

    if path is None:
        path = os.getenv(
            "PAYMENT_LINKS_BOOTSTRAP_PATH",
            str(Path(__file__).resolve().parent.parent / "data" / "bootstrap_payment_links.json"),
        )

    bootstrap = Path(path)
    if not bootstrap.is_file():
        return 0

    try:
        records = json.loads(bootstrap.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("payment_links_bootstrap_read_failed", extra={"error": str(exc)})
        return 0

    if not isinstance(records, list):
        return 0

    init_table()
    imported = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        try:
            invoice_id = str(rec["invoice_id"])
            tap_charge_id = str(rec["tap_charge_id"])
            existing = get_by_invoice_id(invoice_id)
            if existing and existing.get("tap_charge_id") != tap_charge_id:
                delete_by_invoice_id(invoice_id)
            if register_existing_link(
                invoice_id=invoice_id,
                customer_id=str(rec["customer_id"]),
                tap_charge_id=tap_charge_id,
                payment_url=str(rec.get("payment_url") or ""),
                amount=float(rec["amount"]),
                currency=str(rec.get("currency") or "KWD"),
                invoice_number=str(rec.get("invoice_number") or ""),
                customer_name=str(rec.get("customer_name") or ""),
                whatsapp_to_number=str(rec.get("whatsapp_to_number") or ""),
            ):
                imported += 1
        except (KeyError, TypeError, ValueError) as exc:
            _LOG.warning(
                "payment_links_bootstrap_row_skipped",
                extra={"error": str(exc), "invoice_id": rec.get("invoice_id")},
            )

    if imported:
        _LOG.info("payment_links_bootstrap_complete", extra={"imported": imported})
    return imported


def mark_payment_captured(
    invoice_id: str,
    *,
    qbo_payment_id: str = "",
) -> None:
    """Update status to PAYMENT_CAPTURED after QBO payment is created."""
    now = _utcnow()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE payment_links
            SET status = 'PAYMENT_CAPTURED',
                qbo_payment_id = ?,
                payment_captured_at = ?,
                updated_at = ?
            WHERE invoice_id = ?
            """,
            (qbo_payment_id, now, now, invoice_id),
        )
        conn.commit()
    _LOG.info(
        "payment_captured",
        extra={"invoice_id": invoice_id, "qbo_payment_id": qbo_payment_id},
    )


def mark_qbo_updated(invoice_id: str) -> None:
    """Record that the QBO invoice note was successfully updated."""
    now = _utcnow()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE payment_links
            SET qbo_note_updated = 1, qbo_note_updated_at = ?, updated_at = ?
            WHERE invoice_id = ?
            """,
            (now, now, invoice_id),
        )
        conn.commit()
    _LOG.info("qbo_note_marked_updated", extra={"invoice_id": invoice_id})


def mark_whatsapp_sent(
    invoice_id: str, *, message_sid: str, to_number: str
) -> None:
    """Record a successful WhatsApp send."""
    now = _utcnow()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE payment_links
            SET whatsapp_sent = 1, whatsapp_message_sid = ?,
                whatsapp_to_number = ?, whatsapp_sent_at = ?,
                whatsapp_error = NULL, updated_at = ?
            WHERE invoice_id = ?
            """,
            (message_sid, to_number, now, now, invoice_id),
        )
        conn.commit()
    _LOG.info("whatsapp_marked_sent", extra={"invoice_id": invoice_id, "sid": message_sid})


def mark_whatsapp_failed(invoice_id: str, *, error: str) -> None:
    """Record a failed WhatsApp attempt (does not block the payment link)."""
    now = _utcnow()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE payment_links
            SET whatsapp_error = ?, updated_at = ?
            WHERE invoice_id = ?
            """,
            (error[:500], now, invoice_id),
        )
        conn.commit()
    _LOG.warning("whatsapp_marked_failed", extra={"invoice_id": invoice_id})


# ── reads ─────────────────────────────────────────────────────────────────────

def delete_by_invoice_id(invoice_id: str) -> bool:
    """Remove a payment link record (e.g. stale Tap charge). Returns True if deleted."""
    init_table()
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM payment_links WHERE invoice_id = ?",
            (invoice_id,),
        )
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        _LOG.info("payment_link_deleted", extra={"invoice_id": invoice_id})
    return deleted


def get_by_invoice_id(invoice_id: str) -> Optional[dict]:
    """Return the payment link record for a QBO invoice ID, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM payment_links WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
    return dict(row) if row else None


def get_by_charge_id(tap_charge_id: str) -> Optional[dict]:
    """Look up by Tap charge ID (useful for webhook processing in later phases)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM payment_links WHERE tap_charge_id = ?", (tap_charge_id,)
        ).fetchone()
    return dict(row) if row else None


# ── helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_by_invoice_number(invoice_number: str) -> Optional[dict]:
    """Return the payment link record for a QBO DocNumber, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM payment_links WHERE invoice_number = ?", (invoice_number,)
        ).fetchone()
    return dict(row) if row else None
