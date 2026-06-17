"""Offline tests for the invoice_to_tap workflow.

No real QBO, Tap, or database calls — everything is mocked.
Run with:  python -m pytest tests/test_invoice_to_tap_workflow.py -v
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from config import Settings
from workflows.invoice_to_tap import (
    LinkResult, SkipResult,
    _build_private_note, _phone_for_tap, _split_name, _validate_invoice,
    process_invoice,
)
from tap.models import TapPhoneNumber
from db import payment_links as db_module
import workflows.invoice_to_tap as wf_module


# ── helpers ───────────────────────────────────────────────────────────────────

def _settings() -> Settings:
    return Settings(
        client_id="id", client_secret="sec",
        redirect_uri="http://localhost:8000/callback",
        environment="production", minor_version="75",
        token_path="tokens.json", realm_id=None,
        webhook_verifier_token=None,
        tap_secret_key="sk_live_test",
        tap_redirect_url="https://hub.example.com/callback",
        tap_webhook_url="https://hub.example.com/webhook/tap",
        database_path=":memory:",
    )


def _qbo_invoice(balance=250.0, total=250.0, doc_number="1089"):
    return {
        "Id": "42", "SyncToken": "3",
        "DocNumber": doc_number,
        "TotalAmt": total, "Balance": balance,
        "CustomerRef": {"value": "99", "name": "Al-Rashid Co"},
        "PrivateNote": "",
    }


def _qbo_customer():
    from qbo.models import Customer
    return Customer(
        id="99", display_name="Ahmed Al-Rashid",
        email="ahmed@example.com",
        phone="+96524001234",          # landline (for phone fallback test)
        mobile="+96565068000",         # valid mobile
        alternate_phone="",
    )


def _tap_invoice_resp():
    from tap.models import InvoiceResponse
    return InvoiceResponse(
        id="inv_TS07test",
        url="https://tap.company/invoice/inv_TS07test",
        status="CREATED",
        amount=250.0,
        currency="KWD",
        tap_customer_id="cus_test",
    )


def _make_workflow_mocks(monkeypatch, *, existing_link=None):
    """Return (qbo_mock, tap_mock) with sensible defaults, DB ops patched."""
    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda inv_id: existing_link)
    monkeypatch.setattr(db_module, "create_link", lambda **kw: {"tap_charge_id": kw["tap_charge_id"]})
    monkeypatch.setattr(db_module, "mark_qbo_updated", lambda inv_id: None)

    qbo = MagicMock()
    qbo.get_invoice.return_value = _qbo_invoice()
    qbo.get_customer_by_id.return_value = _qbo_customer()
    qbo.update_invoice_note.return_value = {}

    tap = MagicMock()
    tap.create_tap_invoice.return_value = _tap_invoice_resp()

    return qbo, tap


# ── unit helpers ──────────────────────────────────────────────────────────────

class TestSplitName:
    def test_two_words(self):     assert _split_name("Ahmed Al-Rashid") == ("Ahmed", "Al-Rashid")
    def test_single_word(self):   assert _split_name("Ahmed") == ("Ahmed", ".")
    def test_three_words(self):   assert _split_name("Al Rashid Co") == ("Al", "Rashid Co")


class TestPhoneForTap:
    def test_prefers_mobile(self):
        c = _qbo_customer()
        ph = _phone_for_tap(c)
        assert ph.country_code == "965"
        assert ph.number == "65068000"

    def test_falls_back_to_primary_phone(self):
        from qbo.models import Customer
        c = Customer(id="1", display_name="X", email="", phone="+96565099999", mobile="", alternate_phone="")
        ph = _phone_for_tap(c)
        assert ph.number == "65099999"

    def test_returns_empty_when_no_valid_mobile(self):
        from qbo.models import Customer
        c = Customer(id="1", display_name="X", email="", phone="24123456", mobile="", alternate_phone="")
        ph = _phone_for_tap(c)
        # 24123456 is a landline — should NOT be returned as Tap phone
        assert ph.country_code == ""


class TestValidateInvoice:
    def test_open_invoice_passes(self):
        assert _validate_invoice(_qbo_invoice(balance=250.0, total=250.0)) is None

    def test_paid_invoice_skipped(self):
        skip = _validate_invoice(_qbo_invoice(balance=0.0, total=250.0))
        assert isinstance(skip, SkipResult)
        assert skip.reason == "ALREADY_PAID"

    def test_voided_invoice_skipped(self):
        skip = _validate_invoice(_qbo_invoice(balance=0.0, total=0.0))
        assert isinstance(skip, SkipResult)
        assert skip.reason == "VOIDED"

    def test_partial_payment_not_skipped(self):
        assert _validate_invoice(_qbo_invoice(balance=50.0, total=250.0)) is None


class TestBuildPrivateNote:
    def test_appends_to_existing_note(self):
        note = _build_private_note("Existing content", "https://pay.url", "chg_x", "2026-06-06T12:00:00Z")
        assert "Existing content" in note
        assert "Prime Automation Hub" in note
        assert "https://pay.url" in note
        assert "chg_x" in note

    def test_no_existing_note(self):
        note = _build_private_note("", "https://pay.url", "chg_x", "2026-06-06T12:00:00Z")
        assert not note.startswith("\n")
        assert "https://pay.url" in note


# ── full workflow ─────────────────────────────────────────────────────────────

class TestProcessInvoice:
    def test_happy_path_generates_link(self, monkeypatch):
        qbo, tap = _make_workflow_mocks(monkeypatch)
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, LinkResult)
        assert result.status == "LINK_GENERATED"
        assert result.tap_charge_id == "inv_TS07test"
        assert "tap.company/invoice" in result.payment_url
        assert result.invoice_id == "42"

    def test_happy_path_calls_qbo_update(self, monkeypatch):
        qbo, tap = _make_workflow_mocks(monkeypatch)
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, LinkResult)
        assert result.qbo_note_updated is True
        qbo.update_invoice_note.assert_called_once()

    def test_idempotency_returns_existing_link(self, monkeypatch):
        existing = {
            "invoice_id": "42", "tap_charge_id": "inv_existing", "invoice_number": "1089",
            "customer_name": "Al-Rashid", "amount": 250.0, "currency": "KWD",
            "payment_url": "https://old.link", "qbo_note_updated": 1,
            "created_at": "2026-01-01T00:00:00Z",
        }
        qbo, tap = _make_workflow_mocks(monkeypatch, existing_link=existing)
        tap.get_tap_invoice.return_value = {"id": "inv_existing", "status": "CREATED"}
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, LinkResult)
        assert result.status == "EXISTING_LINK_RETURNED"
        assert result.payment_url == "https://old.link"
        tap.create_tap_invoice.assert_not_called()
        qbo.get_invoice.assert_called_once()

    def test_uses_balance_not_total_for_tap_charge(self, monkeypatch):
        qbo, tap = _make_workflow_mocks(monkeypatch)
        qbo.get_invoice.return_value = _qbo_invoice(balance=100.0, total=200.0)
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, LinkResult)
        assert result.amount == 100.0
        tap.create_tap_invoice.assert_called_once()
        assert tap.create_tap_invoice.call_args[0][0].amount == 100.0

    def test_balance_change_regenerates_link(self, monkeypatch):
        existing = {
            "invoice_id": "42", "tap_charge_id": "inv_existing", "invoice_number": "1089",
            "customer_name": "Al-Rashid", "amount": 200.0, "currency": "KWD",
            "payment_url": "https://old.link", "qbo_note_updated": 1,
            "created_at": "2026-01-01T00:00:00Z",
        }
        deleted: list[str] = []
        qbo, tap = _make_workflow_mocks(monkeypatch, existing_link=existing)
        qbo.get_invoice.return_value = _qbo_invoice(balance=100.0, total=200.0)
        tap.get_tap_invoice.return_value = {"id": "inv_existing", "status": "CREATED"}
        monkeypatch.setattr(
            db_module, "delete_by_invoice_id",
            lambda inv_id: deleted.append(inv_id) or True,
        )
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, LinkResult)
        assert result.status == "LINK_GENERATED"
        assert result.amount == 100.0
        assert deleted == ["42"]
        tap.create_tap_invoice.assert_called_once()

    def test_stale_charge_regenerates_link(self, monkeypatch):
        existing = {
            "invoice_id": "42", "tap_charge_id": "inv_stale", "invoice_number": "1089",
            "customer_name": "Al-Rashid", "amount": 250.0, "currency": "KWD",
            "payment_url": "https://old.link", "qbo_note_updated": 1,
            "created_at": "2026-01-01T00:00:00Z",
        }
        qbo, tap = _make_workflow_mocks(monkeypatch, existing_link=existing)
        tap.get_tap_invoice.return_value = {"id": "inv_stale", "status": "EXPIRED"}
        deleted = []

        def fake_delete(invoice_id):
            deleted.append(invoice_id)
            return True

        monkeypatch.setattr(db_module, "delete_by_invoice_id", fake_delete)
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, LinkResult)
        assert result.status == "LINK_GENERATED"
        assert result.tap_charge_id == "inv_TS07test"
        assert deleted == ["42"]
        tap.create_tap_invoice.assert_called_once()

    def test_paid_invoice_returns_skip_result(self, monkeypatch):
        qbo, tap = _make_workflow_mocks(monkeypatch)
        qbo.get_invoice.return_value = _qbo_invoice(balance=0.0, total=250.0)
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, SkipResult)
        assert result.reason == "ALREADY_PAID"
        tap.create_tap_invoice.assert_not_called()

    def test_voided_invoice_returns_skip_result(self, monkeypatch):
        qbo, tap = _make_workflow_mocks(monkeypatch)
        qbo.get_invoice.return_value = _qbo_invoice(balance=0.0, total=0.0)
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, SkipResult)
        assert result.reason == "VOIDED"

    def test_bank_transfer_customer_skips_tap_charge(self, monkeypatch):
        from qbo.models import Customer

        qbo, tap = _make_workflow_mocks(monkeypatch)
        qbo.get_customer_by_id.return_value = Customer(
            id="99", display_name="Ahmed Al-Rashid",
            email="ahmed@example.com",
            phone="+96524001234",
            mobile="+96565068000",
            alternate_phone="",
            notes="BANK_TRANSFER",
        )
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, SkipResult)
        assert result.reason == "BANK_TRANSFER"
        assert "bank transfer" in result.detail.lower()
        tap.create_tap_invoice.assert_not_called()

    def test_bank_transfer_sends_whatsapp_with_bank_info(self, monkeypatch):
        from qbo.models import Customer
        from messaging.whatsapp import MessageResult

        qbo, tap = _make_workflow_mocks(monkeypatch)
        qbo.get_customer_by_id.return_value = Customer(
            id="99", display_name="Ahmed Al-Rashid",
            email="ahmed@example.com",
            phone="+96524001234",
            mobile="+96565068000",
            alternate_phone="",
            notes="BANK_TRANSFER",
        )
        wa = MagicMock()
        wa.send_payment_link.return_value = MessageResult(
            to="+96565068000", sid="SM_bank", status="queued"
        )
        from dataclasses import replace

        settings = replace(
            _settings(),
            bank_transfer_info="NBK | Account: 2019015492 | IBAN: KW70NBOK0000000000002019015492",
        )
        result = process_invoice(
            "42", qbo_client=qbo, tap_client=tap, whatsapp_client=wa, settings=settings
        )
        assert isinstance(result, SkipResult)
        assert result.reason == "BANK_TRANSFER"
        tap.create_tap_invoice.assert_not_called()
        wa.send_payment_link.assert_called_once()
        wa.send_admin_bank_notify.assert_called_once_with(
            "1089", "Ahmed Al-Rashid", 250.0, "KWD"
        )
        _, kwargs = wa.send_payment_link.call_args
        assert "NBK" in kwargs["payment_url"]
        assert kwargs["invoice_link"] == "https://prime-automation-hub.onrender.com/invoice/42/pdf"

    def test_bank_transfer_match_is_case_insensitive(self, monkeypatch):
        from qbo.models import Customer

        qbo, tap = _make_workflow_mocks(monkeypatch)
        qbo.get_customer_by_id.return_value = Customer(
            id="99", display_name="Ahmed Al-Rashid",
            email="ahmed@example.com",
            phone="+96524001234",
            mobile="+96565068000",
            alternate_phone="",
            notes="Please use bank_transfer for this account",
        )
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, SkipResult)
        assert result.reason == "BANK_TRANSFER"
        tap.create_tap_invoice.assert_not_called()

    def test_qbo_update_failure_does_not_fail_workflow(self, monkeypatch):
        """Even if QBO PrivateNote update fails, the DB record is saved and result is returned."""
        from qbo.client import QBOError
        qbo, tap = _make_workflow_mocks(monkeypatch)
        qbo.update_invoice_note.side_effect = QBOError("QBO unavailable")
        result = process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())
        assert isinstance(result, LinkResult)
        assert result.qbo_note_updated is False   # flagged but not fatal
        assert result.tap_charge_id == "inv_TS07test"

    def test_tap_failure_does_not_write_to_db(self, monkeypatch):
        """If Tap fails, we must not insert a partial record into the DB."""
        from tap.exceptions import TapServerError
        db_create_called = [False]

        def fake_create_link(**kw):
            db_create_called[0] = True

        qbo, tap = _make_workflow_mocks(monkeypatch)
        monkeypatch.setattr(db_module, "create_link", fake_create_link)
        tap.create_tap_invoice.side_effect = TapServerError("Server down", status_code=503)

        with pytest.raises(TapServerError):
            process_invoice("42", qbo_client=qbo, tap_client=tap, settings=_settings())

        assert db_create_called[0] is False   # DB untouched
