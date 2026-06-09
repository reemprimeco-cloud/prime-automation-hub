"""Offline tests for the Tap Payments client.

No real Tap API calls — requests.Session.request is monkeypatched.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from tap.client import TapClient, _MAX_RETRIES
from tap.exceptions import (
    TapAuthError, TapClientError, TapError,
    TapParseError, TapRateLimitError, TapServerError,
)
from tap.models import CreateChargeRequest, TapCustomer, TapPhoneNumber

# ── fixtures ──────────────────────────────────────────────────────────────────

TOKEN = "sk_live_test_key"

GOOD_CHARGE_RESPONSE = {
    "id": "chg_TS07A5020231643Obe10906052",
    "object": "charge",
    "status": "INITIATED",
    "amount": 250.000,
    "currency": "KWD",
    "transaction": {
        "url": "https://checkout.tap.company/v2/session/chg_TS07",
        "expiry": {"period": 7, "type": "D"},
    },
    "customer": {"id": "cus_TS01A_xxx"},
}


def _make_client() -> TapClient:
    return TapClient(TOKEN)


def _make_request(**overrides) -> CreateChargeRequest:
    defaults = dict(
        amount=250.000,
        currency="KWD",
        description="Invoice #1089 — Acme",
        customer=TapCustomer(
            first_name="Ahmed",
            last_name="Al-Rashid",
            email="ahmed@acme.com",
            phone=TapPhoneNumber("965", "65068000"),
        ),
        transaction_ref="INV-1089",
        order_ref="42",
        redirect_url="https://hub.example.com/callback",
        webhook_url="https://hub.example.com/webhook/tap",
        metadata={"qbo_invoice_id": "42"},
    )
    defaults.update(overrides)
    return CreateChargeRequest(**defaults)


def _mock_resp(status_code: int, body: dict | str) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.ok = 200 <= status_code < 300
    if isinstance(body, dict):
        r.json.return_value = body
        r.text = json.dumps(body)
    else:
        r.json.side_effect = ValueError("not json")
        r.text = body
    return r


# ── request body construction ─────────────────────────────────────────────────

class TestBuildChargeBody:
    def test_required_fields_present(self):
        req = _make_request()
        body = TapClient._build_charge_body(req)
        assert body["amount"] == 250.000
        assert body["currency"] == "KWD"
        assert body["source"]["id"] == "src_all"
        assert body["redirect"]["url"] == "https://hub.example.com/callback"
        assert body["post"]["url"] == "https://hub.example.com/webhook/tap"

    def test_customer_name_and_phone(self):
        req = _make_request()
        body = TapClient._build_charge_body(req)
        assert body["customer"]["first_name"] == "Ahmed"
        assert body["customer"]["last_name"]  == "Al-Rashid"
        assert body["customer"]["phone"] == {"country_code": "965", "number": "65068000"}

    def test_phone_omitted_when_empty(self):
        req = _make_request(customer=TapCustomer(
            first_name="Bob", last_name=".", email="b@b.com",
            phone=TapPhoneNumber("", ""),
        ))
        body = TapClient._build_charge_body(req)
        assert "phone" not in body["customer"]

    def test_reference_fields(self):
        body = TapClient._build_charge_body(_make_request())
        assert body["reference"]["transaction"] == "INV-1089"
        assert body["reference"]["order"]       == "42"

    def test_expiry_is_7_days(self):
        body = TapClient._build_charge_body(_make_request())
        assert body["expiry"] == {"period": 7, "type": "D"}
        assert body["transaction"]["expiry"] == {"period": 7, "type": "D"}

    def test_metadata_forwarded(self):
        body = TapClient._build_charge_body(_make_request())
        assert body["metadata"]["qbo_invoice_id"] == "42"

    def test_amount_rounded_to_3dp(self):
        req = _make_request(amount=250.99999)
        body = TapClient._build_charge_body(req)
        assert body["amount"] == 251.000  # rounded


# ── response parsing ──────────────────────────────────────────────────────────

class TestParseChargeResponse:
    def test_parses_valid_initiated_response(self):
        resp = TapClient._parse_charge_response(GOOD_CHARGE_RESPONSE)
        assert resp.charge_id    == "chg_TS07A5020231643Obe10906052"
        assert resp.payment_url  == "https://checkout.tap.company/v2/session/chg_TS07"
        assert resp.status       == "INITIATED"
        assert resp.amount       == 250.000
        assert resp.currency     == "KWD"
        assert resp.tap_customer_id == "cus_TS01A_xxx"

    def test_raises_parse_error_when_id_missing(self):
        with pytest.raises(TapParseError, match="missing 'id'"):
            TapClient._parse_charge_response({})

    def test_raises_parse_error_when_initiated_has_no_url(self):
        bad = {**GOOD_CHARGE_RESPONSE, "transaction": {}}
        with pytest.raises(TapParseError, match="transaction.url"):
            TapClient._parse_charge_response(bad)

    def test_does_not_raise_for_non_initiated_without_url(self):
        # A CAPTURED response won't have transaction.url — that's OK
        captured = {**GOOD_CHARGE_RESPONSE, "status": "CAPTURED", "transaction": {}}
        resp = TapClient._parse_charge_response(captured)
        assert resp.status == "CAPTURED"
        assert resp.payment_url == ""


# ── HTTP layer: success ───────────────────────────────────────────────────────

def test_create_charge_returns_response(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client._session, "post", lambda *a, **kw: _mock_resp(200, GOOD_CHARGE_RESPONSE))
    resp = client.create_charge(_make_request())
    assert resp.charge_id   == "chg_TS07A5020231643Obe10906052"
    assert "checkout.tap.company" in resp.payment_url


# ── HTTP layer: error handling ────────────────────────────────────────────────

def test_raises_tap_auth_error_on_401(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client._session, "post", lambda *a, **kw: _mock_resp(401, {"errors": [{"description": "Invalid key"}]}))
    with pytest.raises(TapAuthError):
        client.create_charge(_make_request())


def test_raises_tap_client_error_on_400(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client._session, "post", lambda *a, **kw: _mock_resp(400, {"errors": [{"description": "Bad amount"}]}))
    with pytest.raises(TapClientError):
        client.create_charge(_make_request())


def test_retries_on_503_then_succeeds(monkeypatch):
    client = _make_client()
    calls = []

    def fake_post(*a, **kw):
        calls.append(1)
        if len(calls) < 2:
            return _mock_resp(503, "Service unavailable")
        return _mock_resp(200, GOOD_CHARGE_RESPONSE)

    monkeypatch.setattr(client._session, "post", fake_post)
    monkeypatch.setattr("tap.client.time.sleep", lambda _: None)

    resp = client.create_charge(_make_request())
    assert len(calls) == 2
    assert resp.charge_id == "chg_TS07A5020231643Obe10906052"


def test_raises_server_error_after_max_retries(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client._session, "post", lambda *a, **kw: _mock_resp(500, "Internal error"))
    monkeypatch.setattr("tap.client.time.sleep", lambda _: None)
    with pytest.raises(TapServerError):
        client.create_charge(_make_request())


def test_no_retry_on_client_error_400(monkeypatch):
    """Client errors (4xx) must not be retried — only one POST call expected."""
    client = _make_client()
    call_count = [0]

    def fake_post(*a, **kw):
        call_count[0] += 1
        return _mock_resp(422, {"errors": [{"description": "Unprocessable"}]})

    monkeypatch.setattr(client._session, "post", fake_post)
    with pytest.raises(TapClientError):
        client.create_charge(_make_request())
    assert call_count[0] == 1   # no retries


def test_raises_tap_error_when_no_secret_key():
    with pytest.raises(TapError, match="TAP_SECRET_KEY"):
        TapClient("")
