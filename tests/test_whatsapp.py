"""Offline tests for WhatsApp sending.

All Twilio API calls are mocked — no real messages are sent.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from messaging.whatsapp import MessageResult, WhatsAppClient


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_client() -> WhatsAppClient:
    with patch("twilio.rest.Client"):
        client = WhatsAppClient(
            account_sid="AC_test",
            auth_token="auth_test",
            from_number="+96565000000",
            content_sid="HXtest123",
        )
    return client


def _mock_twilio_message(sid="SM123", status="queued"):
    msg = MagicMock()
    msg.sid = sid
    msg.status = status
    return msg


# ── MessageResult ─────────────────────────────────────────────────────────────

def test_message_result_sent_when_sid_present():
    r = MessageResult(to="+96565068000", sid="SM123", status="queued")
    assert r.sent is True


def test_message_result_not_sent_when_error():
    r = MessageResult(to="+96565068000", error="failed")
    assert r.sent is False


def test_message_result_not_sent_when_no_sid():
    r = MessageResult(to="+96565068000")
    assert r.sent is False


# ── WhatsAppClient ────────────────────────────────────────────────────────────

def test_send_uses_content_sid():
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = _mock_twilio_message()
        client = WhatsAppClient("AC", "auth", "+96565000000", "HXtest")
        result = client.send_payment_link(
            "+96565068000",
            customer_name="Ahmed Al-Rashid",
            invoice_number="2527",
            amount=48.0,
            payment_url="https://checkout.tap.company/test",
        )
        call_kwargs = mock_instance.messages.create.call_args.kwargs
        assert call_kwargs["content_sid"] == "HXtest"
        assert call_kwargs["from_"] == "whatsapp:+96565000000"
        assert call_kwargs["to"] == "whatsapp:+96565068000"


def test_send_variables_include_all_four():
    import json
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = _mock_twilio_message()
        client = WhatsAppClient("AC", "auth", "+96565000000", "HXtest")
        client.send_payment_link(
            "+96565068000",
            customer_name="Dar Haa",
            invoice_number="2527",
            amount=48.0,
            payment_url="https://checkout.tap.company/test",
            invoice_link="https://connect.intuit.com/portal/app/invoice/view/123",
        )
        call_kwargs = mock_instance.messages.create.call_args.kwargs
        variables = json.loads(call_kwargs["content_variables"])
        assert variables["1"] == "Dar"          # first name
        assert variables["2"] == "2527"
        assert "48.000 KWD" in variables["3"]
        assert variables["4"] == "https://checkout.tap.company/test"
        assert variables["5"] == "https://connect.intuit.com/portal/app/invoice/view/123"


def test_send_variables_use_na_when_invoice_link_missing():
    import json
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = _mock_twilio_message()
        client = WhatsAppClient("AC", "auth", "+96565000000", "HXtest")
        client.send_payment_link(
            "+96565068000",
            customer_name="Dar Haa",
            invoice_number="2527",
            amount=48.0,
            payment_url="https://checkout.tap.company/test",
        )
        call_kwargs = mock_instance.messages.create.call_args.kwargs
        variables = json.loads(call_kwargs["content_variables"])
        assert variables["5"] == "N/A"


def test_send_returns_message_result_on_success():
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.return_value = _mock_twilio_message("SM_success", "queued")
        client = WhatsAppClient("AC", "auth", "+96565000000", "HXtest")
        result = client.send_payment_link(
            "+96565068000",
            customer_name="Ahmed",
            invoice_number="1",
            amount=10.0,
            payment_url="https://tap.test",
        )
        assert result.sent is True
        assert result.sid == "SM_success"


def test_send_returns_error_result_on_twilio_exception():
    with patch("twilio.rest.Client") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.messages.create.side_effect = Exception("Twilio error")
        client = WhatsAppClient("AC", "auth", "+96565000000", "HXtest")
        result = client.send_payment_link(
            "+96565068000",
            customer_name="Ahmed",
            invoice_number="1",
            amount=10.0,
            payment_url="https://tap.test",
        )
        assert result.sent is False
        assert "Twilio error" in result.error


def test_missing_credentials_raises():
    with patch("twilio.rest.Client"):
        with pytest.raises(ValueError, match="required"):
            WhatsAppClient("", "auth", "+96565000000", "HXtest")


# ── workflow WhatsApp step ────────────────────────────────────────────────────

def test_workflow_sends_whatsapp_on_happy_path(monkeypatch):
    """WhatsApp is sent when customer has a valid mobile."""
    import time
    from config import Settings
    from qbo.models import Customer
    from tap.models import InvoiceResponse
    from workflows.invoice_to_tap import process_invoice, LinkResult
    import db.payment_links as db_module

    settings = Settings(
        client_id="id", client_secret="sec",
        redirect_uri="http://localhost:8000/callback",
        environment="production", minor_version="75",
        token_path="tokens.json", realm_id=None,
        webhook_verifier_token=None,
        twilio_account_sid="AC", twilio_auth_token="auth",
        twilio_whatsapp_from="+96565000000", twilio_content_sid="HXtest",
    )

    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda _: None)
    monkeypatch.setattr(db_module, "create_link", lambda **kw: {"tap_charge_id": "chg_x"})
    monkeypatch.setattr(db_module, "mark_qbo_updated", lambda _: None)
    monkeypatch.setattr(db_module, "mark_whatsapp_sent", lambda inv_id, **kw: None)
    monkeypatch.setattr(db_module, "mark_whatsapp_failed", lambda inv_id, **kw: None)

    qbo = MagicMock()
    qbo.get_invoice.return_value = {
        "Id": "42", "SyncToken": "1", "DocNumber": "1089",
        "TotalAmt": 48.0, "Balance": 48.0,
        "CustomerRef": {"value": "99", "name": "Dar Haa"}, "PrivateNote": "",
        "InvoiceLink": "https://connect.intuit.com/portal/app/invoice/view/42",
    }
    qbo.get_customer_by_id.return_value = Customer(
        id="99", display_name="Dar Haa", email="dar@haa.com",
        phone="", mobile="+96565068000", alternate_phone="",
    )
    qbo.update_invoice_note.return_value = {}

    tap = MagicMock()
    tap.create_tap_invoice.return_value = InvoiceResponse(
        id="inv_test", url="https://tap.test/invoice",
        status="CREATED", amount=48.0, currency="KWD", tap_customer_id="cus_x",
    )

    wa = MagicMock()
    wa.send_payment_link.return_value = MessageResult(
        to="+96565068000", sid="SM_test", status="queued"
    )

    result = process_invoice("42", qbo_client=qbo, tap_client=tap, whatsapp_client=wa, settings=settings)
    assert isinstance(result, LinkResult)
    assert result.whatsapp_sent is True
    assert result.whatsapp_number == "+96565068000"
    wa.send_payment_link.assert_called_once()
    _, kwargs = wa.send_payment_link.call_args
    assert kwargs["invoice_link"] == "https://connect.intuit.com/portal/app/invoice/view/42"
    assert kwargs["payment_url"] == "https://tap.test/invoice"


def test_workflow_skips_whatsapp_when_no_client(monkeypatch):
    """Passing whatsapp_client=None skips WhatsApp without error."""
    from config import Settings
    from qbo.models import Customer
    from tap.models import InvoiceResponse
    from workflows.invoice_to_tap import process_invoice, LinkResult
    import db.payment_links as db_module

    settings = Settings(
        client_id="id", client_secret="sec",
        redirect_uri="http://localhost:8000/callback",
        environment="production", minor_version="75",
        token_path="tokens.json", realm_id=None,
        webhook_verifier_token=None,
    )

    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda _: None)
    monkeypatch.setattr(db_module, "create_link", lambda **kw: {"tap_charge_id": "chg_x"})
    monkeypatch.setattr(db_module, "mark_qbo_updated", lambda _: None)

    qbo = MagicMock()
    qbo.get_invoice.return_value = {
        "Id": "42", "SyncToken": "1", "DocNumber": "1089",
        "TotalAmt": 48.0, "Balance": 48.0,
        "CustomerRef": {"value": "99", "name": "Dar Haa"}, "PrivateNote": "",
        "InvoiceLink": "https://connect.intuit.com/portal/app/invoice/view/42",
    }
    qbo.get_customer_by_id.return_value = Customer(
        id="99", display_name="Dar Haa", email="",
        phone="", mobile="+96565068000", alternate_phone="",
    )
    qbo.update_invoice_note.return_value = {}

    tap = MagicMock()
    tap.create_tap_invoice.return_value = InvoiceResponse(
        id="inv_x", url="https://tap.test/invoice",
        status="CREATED", amount=48.0, currency="KWD", tap_customer_id="",
    )

    result = process_invoice("42", qbo_client=qbo, tap_client=tap, whatsapp_client=None, settings=settings)
    assert isinstance(result, LinkResult)
    assert result.whatsapp_sent is False


def test_workflow_does_not_fail_when_whatsapp_errors(monkeypatch):
    """A Twilio error must not fail the workflow — link is still valid."""
    from config import Settings
    from qbo.models import Customer
    from tap.models import InvoiceResponse
    from workflows.invoice_to_tap import process_invoice, LinkResult
    import db.payment_links as db_module

    settings = Settings(
        client_id="id", client_secret="sec",
        redirect_uri="http://localhost:8000/callback",
        environment="production", minor_version="75",
        token_path="tokens.json", realm_id=None,
        webhook_verifier_token=None,
        twilio_account_sid="AC", twilio_auth_token="auth",
        twilio_whatsapp_from="+96565000000", twilio_content_sid="HXtest",
    )

    monkeypatch.setattr(db_module, "init_table", lambda: None)
    monkeypatch.setattr(db_module, "get_by_invoice_id", lambda _: None)
    monkeypatch.setattr(db_module, "create_link", lambda **kw: {"tap_charge_id": "chg_x"})
    monkeypatch.setattr(db_module, "mark_qbo_updated", lambda _: None)
    monkeypatch.setattr(db_module, "mark_whatsapp_failed", lambda inv_id, **kw: None)

    qbo = MagicMock()
    qbo.get_invoice.return_value = {
        "Id": "42", "SyncToken": "1", "DocNumber": "1089",
        "TotalAmt": 48.0, "Balance": 48.0,
        "CustomerRef": {"value": "99", "name": "Dar Haa"}, "PrivateNote": "",
        "InvoiceLink": "https://connect.intuit.com/portal/app/invoice/view/42",
    }
    qbo.get_customer_by_id.return_value = Customer(
        id="99", display_name="Dar Haa", email="",
        phone="", mobile="+96565068000", alternate_phone="",
    )
    qbo.update_invoice_note.return_value = {}

    tap = MagicMock()
    tap.create_tap_invoice.return_value = InvoiceResponse(
        id="inv_x", url="https://tap.test/invoice",
        status="CREATED", amount=48.0, currency="KWD", tap_customer_id="",
    )

    wa = MagicMock()
    wa.send_payment_link.return_value = MessageResult(to="+96565068000", error="Twilio down")

    result = process_invoice("42", qbo_client=qbo, tap_client=tap, whatsapp_client=wa, settings=settings)
    assert isinstance(result, LinkResult)
    assert result.whatsapp_sent is False
    assert result.tap_charge_id == "inv_x"   # payment link still valid
