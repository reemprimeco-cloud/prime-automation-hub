"""Offline tests for the payment capture workflow."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from messaging.whatsapp import MessageResult
from workflows.payment_capture import (
    CaptureError, CaptureResult, handle_payment_capture,
)
import db.payment_links as db_module


# ── helpers ───────────────────────────────────────────────────────────────────

CHARGE_ID = "chg_test_001"
INVOICE_ID = "9258"

DB_RECORD = {
    "invoice_id": INVOICE_ID,
    "customer_id": "99",
    "invoice_number": "2527",
    "customer_name": "Dar Haa",
    "tap_charge_id": CHARGE_ID,
    "amount": 48.0,
    "currency": "KWD",
    "status": "LINK_GENERATED",
    "whatsapp_to_number": "+96599380654",
    "qbo_payment_id": None,
}

QBO_PAYMENT = {"Id": "PAY-001", "TotalAmt": 48.0}


def _mock_db(monkeypatch, record=None, already_captured=False):
    r = dict(record or DB_RECORD)
    if already_captured:
        r["status"] = "PAYMENT_CAPTURED"
        r["qbo_payment_id"] = "PAY-001"
        r["whatsapp_sent"] = 1
    monkeypatch.setattr(db_module, "get_by_charge_id", lambda _: r)
    monkeypatch.setattr(db_module, "mark_payment_captured", lambda inv_id, **kw: None)
    monkeypatch.setattr(db_module, "mark_whatsapp_sent", lambda inv_id, **kw: None)
    monkeypatch.setattr(db_module, "mark_whatsapp_failed", lambda inv_id, **kw: None)


def _mock_qbo(payment_return=None):
    qbo = MagicMock()
    qbo.create_payment.return_value = payment_return or QBO_PAYMENT
    return qbo


def _mock_wa(sent=True):
    wa = MagicMock()
    wa.send_payment_confirmation.return_value = MessageResult(
        to="+96599380654",
        sid="SM_confirm" if sent else "",
        status="queued" if sent else "",
        error="" if sent else "Twilio error",
    )
    return wa


# ── happy path ────────────────────────────────────────────────────────────────

def test_happy_path_captures_payment(monkeypatch):
    _mock_db(monkeypatch)
    qbo = _mock_qbo()
    wa  = _mock_wa()

    result = handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo, whatsapp_client=wa)

    assert isinstance(result, CaptureResult)
    assert result.status == "PAYMENT_CAPTURED"
    assert result.qbo_payment_id == "PAY-001"
    assert result.qbo_payment_created is True
    qbo.create_payment.assert_called_once()


def test_happy_path_sends_confirmation(monkeypatch):
    _mock_db(monkeypatch)
    qbo = _mock_qbo()
    wa  = _mock_wa(sent=True)

    result = handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo, whatsapp_client=wa)

    assert result.whatsapp_sent is True
    assert result.whatsapp_number == "+96599380654"
    wa.send_payment_confirmation.assert_called_once()


def test_happy_path_marks_db_captured(monkeypatch):
    captured = [False]
    _mock_db(monkeypatch)
    monkeypatch.setattr(
        db_module, "mark_payment_captured",
        lambda inv_id, **kw: captured.__setitem__(0, True)
    )
    qbo = _mock_qbo()
    handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo)
    assert captured[0] is True


# ── idempotency ───────────────────────────────────────────────────────────────

def test_already_captured_returns_existing(monkeypatch):
    _mock_db(monkeypatch, already_captured=True)
    qbo = _mock_qbo()

    result = handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo)

    assert isinstance(result, CaptureResult)
    assert result.status == "ALREADY_CAPTURED"
    qbo.create_payment.assert_not_called()


# ── error cases ───────────────────────────────────────────────────────────────

def test_unknown_charge_returns_error(monkeypatch):
    monkeypatch.setattr(db_module, "get_by_charge_id", lambda _: None)
    monkeypatch.setattr(db_module, "init_table", lambda: None)

    tap = MagicMock()
    tap.get_charge.return_value = {"metadata": {}}
    monkeypatch.setattr("config.get_settings", lambda: MagicMock())
    monkeypatch.setattr("tap.client.tap_client_from_settings", lambda _: tap)

    qbo = _mock_qbo()

    result = handle_payment_capture("chg_unknown", 48.0, qbo_client=qbo)

    assert isinstance(result, CaptureError)
    assert result.reason == "CHARGE_NOT_FOUND"
    qbo.create_payment.assert_not_called()


def test_recovers_from_tap_metadata_when_db_missing(monkeypatch):
    monkeypatch.setattr(db_module, "get_by_charge_id", lambda _: None)
    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "mark_payment_captured", lambda inv_id, **kw: None)

    tap = MagicMock()
    tap.get_charge.return_value = {
        "metadata": {
            "qbo_invoice_id": INVOICE_ID,
            "invoice_number": "2527",
            "qbo_customer_id": "99",
        }
    }
    monkeypatch.setattr("config.get_settings", lambda: MagicMock())
    monkeypatch.setattr("tap.client.tap_client_from_settings", lambda _: tap)

    qbo = _mock_qbo()
    result = handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo)

    assert isinstance(result, CaptureResult)
    assert result.status == "PAYMENT_CAPTURED"
    assert result.invoice_id == INVOICE_ID
    qbo.create_payment.assert_called_once()


def test_recovers_from_tap_invoice_when_id_starts_with_inv(monkeypatch):
    monkeypatch.setattr(db_module, "get_by_charge_id", lambda _: None)
    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "mark_payment_captured", lambda inv_id, **kw: None)

    tap = MagicMock()
    tap.get_tap_invoice.return_value = {
        "metadata": {
            "qbo_invoice_id": INVOICE_ID,
            "invoice_number": "2527",
            "qbo_customer_id": "99",
        }
    }
    monkeypatch.setattr("config.get_settings", lambda: MagicMock())
    monkeypatch.setattr("tap.client.tap_client_from_settings", lambda _: tap)

    qbo = _mock_qbo()
    result = handle_payment_capture("inv_test123", 48.0, qbo_client=qbo)

    assert isinstance(result, CaptureResult)
    tap.get_tap_invoice.assert_called_once_with("inv_test123")
    tap.get_charge.assert_not_called()


def test_charge_webhook_finds_db_record_by_qbo_invoice_id(monkeypatch):
    db_record = dict(DB_RECORD)
    db_record["tap_charge_id"] = "inv_stored123"

    monkeypatch.setattr(db_module, "get_by_charge_id", lambda _: None)
    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda inv_id: db_record if inv_id == INVOICE_ID else None)
    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "mark_payment_captured", lambda inv_id, **kw: None)

    qbo = _mock_qbo()
    payload = {
        "id": "chg_transaction123",
        "status": "CAPTURED",
        "reference": {"order": INVOICE_ID, "invoice": "INV-2527"},
        "metadata": {
            "qbo_invoice_id": INVOICE_ID,
            "invoice_number": "2527",
            "qbo_customer_id": "99",
        },
    }
    result = handle_payment_capture(
        "chg_transaction123",
        48.0,
        qbo_client=qbo,
        tap_webhook_payload=payload,
    )

    assert isinstance(result, CaptureResult)
    assert result.status == "PAYMENT_CAPTURED"
    qbo.create_payment.assert_called_once()


def test_charge_webhook_resolves_inv_id_from_payload(monkeypatch):
    db_record = dict(DB_RECORD)
    db_record["tap_charge_id"] = "inv_parent123"

    monkeypatch.setattr(
        db_module,
        "get_by_charge_id",
        lambda charge_id: db_record if charge_id == "inv_parent123" else None,
    )
    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "mark_payment_captured", lambda inv_id, **kw: None)

    tap = MagicMock()
    tap.get_charge.side_effect = Exception("404 charge not found")
    monkeypatch.setattr("config.get_settings", lambda: MagicMock())
    monkeypatch.setattr("tap.client.tap_client_from_settings", lambda _: tap)

    qbo = _mock_qbo()
    payload = {
        "id": "chg_transaction123",
        "invoice_id": "inv_parent123",
        "status": "CAPTURED",
    }
    result = handle_payment_capture(
        "chg_transaction123",
        48.0,
        qbo_client=qbo,
        tap_webhook_payload=payload,
    )

    assert isinstance(result, CaptureResult)
    assert result.invoice_id == INVOICE_ID
    assert result.status == "PAYMENT_CAPTURED"
    tap.get_charge.assert_not_called()


def test_qbo_failure_returns_error(monkeypatch):
    from qbo.client import QBOError
    _mock_db(monkeypatch)
    qbo = MagicMock()
    qbo.create_payment.side_effect = QBOError("QBO unavailable")

    result = handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo)

    assert isinstance(result, CaptureError)
    assert result.reason == "QBO_ERROR"


def test_qbo_failure_does_not_update_db(monkeypatch):
    from qbo.client import QBOError
    captured = [False]
    _mock_db(monkeypatch)
    monkeypatch.setattr(
        db_module, "mark_payment_captured",
        lambda inv_id, **kw: captured.__setitem__(0, True)
    )
    qbo = MagicMock()
    qbo.create_payment.side_effect = QBOError("QBO down")

    handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo)
    assert captured[0] is False   # DB not updated if QBO fails


def test_whatsapp_failure_does_not_fail_capture(monkeypatch):
    _mock_db(monkeypatch)
    qbo = _mock_qbo()
    wa  = _mock_wa(sent=False)

    result = handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo, whatsapp_client=wa)

    assert isinstance(result, CaptureResult)
    assert result.status == "PAYMENT_CAPTURED"
    assert result.whatsapp_sent is False
    assert result.qbo_payment_created is True   # payment still created


def test_no_whatsapp_client_skips_confirmation(monkeypatch):
    _mock_db(monkeypatch)
    qbo = _mock_qbo()

    result = handle_payment_capture(CHARGE_ID, 48.0, qbo_client=qbo, whatsapp_client=None)

    assert isinstance(result, CaptureResult)
    assert result.whatsapp_sent is False
    assert result.qbo_payment_created is True
