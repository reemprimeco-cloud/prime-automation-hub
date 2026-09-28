"""Offline unit tests for the UPayments client (no live sandbox calls)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest

from upayments.client import UPaymentsClient, _normalize_customer_name, _normalize_phone
from upayments.exceptions import UPaymentsAPIError


@pytest.fixture()
def client() -> UPaymentsClient:
    return UPaymentsClient(
        api_key="test-key",
        merchant_id="78508",
        api_secret="test-api-secret",
        base_url="https://sandboxapi.upayments.com/api/v1",
        return_url="https://example.com/success",
        cancel_url="https://example.com/cancel",
        notification_url="https://example.com/notify",
        webhook_secret="whsec_test",
    )


def test_normalize_customer_name_preserves_short_names():
    # No 3-character minimum — single-letter / short names are OK for UPayments
    assert _normalize_customer_name("Al") == "Al"
    assert _normalize_customer_name("A") == "A"
    assert _normalize_customer_name("") == "Customer"
    assert _normalize_customer_name("  ") == "Customer"


def test_normalize_phone_e164():
    assert _normalize_phone("+965 50-123-456") == "+96550123456"
    assert _normalize_phone("96550123456") == "+96550123456"
    assert _normalize_phone("") == ""


def test_create_charge_posts_expected_body(client: UPaymentsClient, monkeypatch):
    captured = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 201
        resp.text = "{}"
        resp.json.return_value = {
            "status": True,
            "data": {
                "link": "https://sandbox.upayments.com/pay/abc",
                "track_id": "track_abc",
                "amount": 1.0,
                "currency": "KWD",
            },
        }
        return resp

    monkeypatch.setattr(client._session, "request", fake_request)

    result = client.create_charge(
        amount=1.0,
        currency="KWD",
        customer_name="Al",  # short name — must not fail
        customer_phone="+96550123456",
        invoice_number="2555",
        description="Invoice #2555",
    )

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/charge")
    body_str = captured["data"].decode("utf-8")
    body = json.loads(body_str)
    assert body["order"]["amount"] == 1.0
    assert body["order"]["currency"] == "KWD"
    assert body["order"]["id"] == "2555"
    assert body["customer"]["name"] == "Al"
    assert body["customer"]["mobile"] == "+96550123456"
    assert body["notificationUrl"] == "https://example.com/notify"
    assert result["payment_url"] == "https://sandbox.upayments.com/pay/abc"
    assert result["charge_id"] == "track_abc"
    assert result["provider"] == "upayments"

    # HMAC headers: recompute independently and check for an exact match.
    timestamp = captured["headers"]["X-Timestamp"]
    assert timestamp.isdigit()
    expected_payload = f"{timestamp}POSTcharge{body_str}"
    expected_sig = base64.b64encode(
        hmac.new(b"test-api-secret", expected_payload.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    assert captured["headers"]["X-Signature"] == expected_sig


def test_create_charge_raises_on_http_error(client: UPaymentsClient, monkeypatch):
    def fake_request(method, url, data=None, headers=None, timeout=None):
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 401
        resp.text = '{"message":"Unauthenticated"}'
        resp.json.return_value = {"message": "Unauthenticated"}
        return resp

    monkeypatch.setattr(client._session, "request", fake_request)

    with pytest.raises(UPaymentsAPIError) as exc_info:
        client.create_charge(
            1.0, "KWD", "Test", "+96550000000", "1", "desc"
        )
    assert exc_info.value.status_code == 401
    assert "401" in str(exc_info.value)


def test_get_charge_status(client: UPaymentsClient, monkeypatch):
    captured = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        captured["method"] = method
        captured["data"] = data
        captured["headers"] = headers
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.text = "{}"
        resp.json.return_value = {
            "status": True,
            "data": {"result": "CAPTURED", "track_id": "t1"},
        }
        return resp

    monkeypatch.setattr(client._session, "request", fake_request)
    result = client.get_charge_status("t1")
    assert result["data"]["result"] == "CAPTURED"

    # GET requests sign an empty-string body, per spec, and send no body.
    assert captured["method"] == "GET"
    assert captured["data"] is None
    timestamp = captured["headers"]["X-Timestamp"]
    expected_payload = f"{timestamp}GETget-payment-status/t1"
    expected_sig = base64.b64encode(
        hmac.new(b"test-api-secret", expected_payload.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    assert captured["headers"]["X-Signature"] == expected_sig


def test_parse_webhook_event_paid(client: UPaymentsClient):
    event = client.parse_webhook_event({
        "payment_id": "1004",
        "result": "CAPTURED",
        "track_id": "track_1",
        "requested_order_id": "2555",
        "amount": "12.500",
        "currency": "KWD",
    })
    assert event.status == "paid"
    assert event.invoice_number == "2555"
    assert event.provider_transaction_id == "track_1"
    assert event.amount == 12.5


def test_parse_webhook_event_failed(client: UPaymentsClient):
    event = client.parse_webhook_event({
        "result": "NOT CAPTURED",
        "track_id": "track_2",
        "requested_order_id": "2556",
    })
    assert event.status == "failed"


def test_verify_webhook_signature_hmac(client: UPaymentsClient):
    payload = b'{"result":"CAPTURED"}'
    digest = hmac.new(b"whsec_test", payload, hashlib.sha256).hexdigest()
    assert client.verify_webhook_signature(payload, digest) is True
    assert client.verify_webhook_signature(payload, f"sha256={digest}") is True
    assert client.verify_webhook_signature(payload, "deadbeef") is False


def test_verify_webhook_notification_token(client: UPaymentsClient):
    # Live UPayments sends x-notification-token as a static merchant token
    assert client.verify_webhook_signature(b"ignored", "whsec_test") is True
    assert client.verify_webhook_signature(b"ignored", "wrong") is False


def test_verify_webhook_signature_no_secret_allows_any_header():
    c = UPaymentsClient(
        api_key="k",
        merchant_id="78508",
        api_secret="s",
        webhook_secret="",
    )
    assert c.verify_webhook_signature(b"{}", "") is True
    assert c.verify_webhook_signature(b"{}", "abc") is True


def test_parse_webhook_event_live_form_shape(client: UPaymentsClient):
    event = client.parse_webhook_event({
        "payment_id": "101620726000342223",
        "result": "CAPTURED",
        "post_date": "0726",
        "tran_id": "620771009962568",
        "ref": "620771030910",
        "track_id": "019f9eb3ad23e23a5e8744219008d63bv2",
        "auth": "337456",
        "order_id": "019f9eb3ad23e23a5e8744219008d63a",
        "requested_order_id": "LIVE-TEST-001",
        "refund_order_id": "019f9eb3ad23e23a5e8744219008d63a",
        "invoice_id": "39686284",
        "payment_type": "knet",
        "payment_method": "knet",
        "transaction_date": "2026-07-26 16:54:03",
        "receipt_id": "019f9eb3ad23e23a5e8744219008d63a",
        "trn_udf": "merchant_id=78508;invoice=LIVE-TEST-001",
    })
    assert event.status == "paid"
    assert event.invoice_number == "LIVE-TEST-001"
    assert event.provider_transaction_id == "019f9eb3ad23e23a5e8744219008d63bv2"


def test_parse_charge_response_session_id_fallback():
    result = UPaymentsClient._parse_charge_response(
        {
            "status": True,
            "data": {
                "link": (
                    "https://apiv2.upayments.com"
                    "?session_id=20261652022607451277286367947581632288419711124707"
                ),
            },
        },
        amount=1.0,
        currency="KWD",
    )
    assert result.charge_id == "20261652022607451277286367947581632288419711124707"
