"""Tests for admin WhatsApp inbound actions."""
from __future__ import annotations

from unittest.mock import MagicMock

from workflows.admin_whatsapp import (
    handle_admin_action,
    normalize_whatsapp_number,
    parse_button_payload,
    resend_customer_whatsapp,
)


def test_normalize_whatsapp_number():
    assert normalize_whatsapp_number("whatsapp:+96550655856") == "+96550655856"
    assert normalize_whatsapp_number("96550655856") == "+96550655856"


def test_parse_button_payload_from_body():
    assert parse_button_payload("PAID_2537", "") == ("PAID", "2537")
    assert parse_button_payload("", "RESEND_2544") == ("RESEND", "2544")
    assert parse_button_payload("hello", "") is None


def test_handle_status_action():
    qbo = MagicMock()
    qbo.find_invoice_by_doc_number.return_value = {
        "Id": "42",
        "DocNumber": "2537",
        "TotalAmt": 160.0,
        "Balance": 160.0,
        "CustomerRef": {"value": "99", "name": "Coded"},
    }
    result = handle_admin_action(
        "STATUS", "2537", qbo_client=qbo, whatsapp_client=None, settings=MagicMock()
    )
    assert result.ok is True
    assert "OPEN" in result.reply
    assert "Coded" in result.reply


def test_handle_paid_action_creates_bank_payment(monkeypatch):
    qbo = MagicMock()
    qbo.find_invoice_by_doc_number.return_value = {
        "Id": "42",
        "DocNumber": "2537",
        "TotalAmt": 160.0,
        "Balance": 160.0,
        "CustomerRef": {"value": "99", "name": "Coded"},
    }
    qbo.create_bank_transfer_payment.return_value = {"Id": "PAY-1"}

    import db.payment_links as db_module

    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda _: None)
    monkeypatch.setattr(db_module, "mark_payment_captured", lambda *a, **k: None)

    result = handle_admin_action(
        "PAID", "2537", qbo_client=qbo, whatsapp_client=None, settings=MagicMock()
    )
    assert result.ok is True
    assert "PAY-1" in result.reply
    qbo.create_bank_transfer_payment.assert_called_once_with(
        customer_id="99", invoice_id="42", amount=160.0
    )


def test_handle_resend_action(monkeypatch):
    from qbo.models import Customer
    from messaging.whatsapp import MessageResult

    qbo = MagicMock()
    qbo.find_invoice_by_doc_number.return_value = {
        "Id": "42",
        "DocNumber": "2537",
        "TotalAmt": 160.0,
        "Balance": 160.0,
        "CustomerRef": {"value": "99", "name": "Coded"},
        "PrivateNote": "Payment Link:\nhttps://tap.test/pay",
    }
    qbo.get_customer_by_id.return_value = Customer(
        id="99", display_name="Coded", email="", phone="",
        mobile="+96565068000", alternate_phone="", notes="",
    )

    wa = MagicMock()
    wa.send_payment_link.return_value = MessageResult(
        to="+96565068000", sid="SM_x", status="queued"
    )

    import db.payment_links as db_module

    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda _: None)
    monkeypatch.setattr(db_module, "mark_whatsapp_sent", lambda *a, **k: None)

    settings = MagicMock()
    settings.bank_transfer_info = ""

    result = handle_admin_action(
        "RESEND", "2537", qbo_client=qbo, whatsapp_client=wa, settings=settings
    )
    assert result.ok is True
    assert "resent" in result.reply.lower()
    wa.send_payment_link.assert_called_once()


def test_resend_bank_transfer_uses_bank_info(monkeypatch):
    from qbo.models import Customer
    from messaging.whatsapp import MessageResult

    qbo = MagicMock()
    qbo.get_customer_by_id.return_value = Customer(
        id="99", display_name="Coded", email="", phone="",
        mobile="+96565068000", alternate_phone="", notes="BANK_TRANSFER",
    )
    wa = MagicMock()
    wa.send_payment_link.return_value = MessageResult(
        to="+96565068000", sid="SM_x", status="queued"
    )

    import db.payment_links as db_module

    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda _: None)
    monkeypatch.setattr(db_module, "mark_whatsapp_sent", lambda *a, **k: None)

    settings = MagicMock()
    settings.bank_transfer_info = "NBK | IBAN: KW70..."

    sent = resend_customer_whatsapp(
        {
            "Id": "42", "DocNumber": "2537", "TotalAmt": 160.0, "Balance": 160.0,
            "CustomerRef": {"value": "99", "name": "Coded"},
        },
        qbo_client=qbo,
        whatsapp_client=wa,
        settings=settings,
    )
    assert sent is True
    _, kwargs = wa.send_payment_link.call_args
    assert "NBK" in kwargs["payment_url"]
