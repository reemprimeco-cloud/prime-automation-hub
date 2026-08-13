"""Offline tests for the webhook layer.

All tests run WITHOUT live credentials or a live server by using:
  - computed HMAC signatures for valid-signature tests
  - a temp SQLite database path (never touches webhook_events.db)
  - FastAPI's TestClient for endpoint tests

Run with:  python -m pytest -q
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from webhook.verify import compute_signature, verify_signature
from webhook.payload import parse_qbo_webhook_entities
from webhook.storage import init_db, store_webhook_payload, count_all_events
import webhook.storage as _storage_mod


# ── helpers ─────────────────────────────────────────────────────────────────

TOKEN = "test-verifier-secret"

SAMPLE_PAYLOAD = {
    "eventNotifications": [
        {
            "realmId": "9341454641234",
            "dataChangeEvent": {
                "entities": [
                    {"name": "Invoice", "id": "42", "operation": "Create",
                     "lastUpdated": "2026-06-06T10:00:00.000-07:00"},
                    {"name": "Invoice", "id": "43", "operation": "Update",
                     "lastUpdated": "2026-06-06T10:01:00.000-07:00"},
                ]
            },
        }
    ]
}
SAMPLE_BYTES = json.dumps(SAMPLE_PAYLOAD, separators=(",", ":")).encode()

CLOUDEVENTS_PAYLOAD = {
    "specversion": "1.0",
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "source": "intuit.dsnExample",
    "type": "qbo.invoice.created.v1",
    "datacontenttype": "application/json",
    "time": "2026-06-06T10:00:00Z",
    "intuitentityid": "99",
    "intuitaccountid": "9130357945907536",
    "data": {
        "entityName": "Invoice",
        "entityId": "99",
        "operation": "Create",
        "lastUpdated": "2026-06-06T10:00:00Z",
    },
}
CLOUDEVENTS_BYTES = json.dumps(CLOUDEVENTS_PAYLOAD, separators=(",", ":")).encode()


# ── payload parsing ───────────────────────────────────────────────────────────

def test_parse_legacy_payload():
    entities = parse_qbo_webhook_entities(SAMPLE_PAYLOAD)
    assert len(entities) == 2
    assert entities[0]["entity_type"] == "Invoice"
    assert entities[0]["operation"] == "Create"
    assert entities[0]["entity_id"] == "42"


def test_parse_cloudevents_payload():
    entities = parse_qbo_webhook_entities(CLOUDEVENTS_PAYLOAD)
    assert len(entities) == 1
    assert entities[0]["entity_type"] == "Invoice"
    assert entities[0]["operation"] == "Create"
    assert entities[0]["entity_id"] == "99"
    assert entities[0]["realm_id"] == "9130357945907536"


# ── signature verification ────────────────────────────────────────────────────

def test_valid_signature_accepted():
    sig, _ = compute_signature(SAMPLE_BYTES, TOKEN)
    assert verify_signature(SAMPLE_BYTES, sig, TOKEN) is True


def test_wrong_token_rejected():
    sig, _ = compute_signature(SAMPLE_BYTES, TOKEN)
    assert verify_signature(SAMPLE_BYTES, sig, "wrong-token") is False


def test_tampered_payload_rejected():
    sig, _ = compute_signature(SAMPLE_BYTES, TOKEN)
    tampered = SAMPLE_BYTES + b" "
    assert verify_signature(tampered, sig, TOKEN) is False


def test_empty_signature_rejected():
    assert verify_signature(SAMPLE_BYTES, "", TOKEN) is False


def test_empty_token_rejected():
    sig, _ = compute_signature(SAMPLE_BYTES, TOKEN)
    assert verify_signature(SAMPLE_BYTES, sig, "") is False


def test_compute_is_deterministic():
    assert compute_signature(SAMPLE_BYTES, TOKEN) == compute_signature(SAMPLE_BYTES, TOKEN)


# ── storage ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Redirect all storage calls to a temp database for each test."""
    db = str(tmp_path / "test_webhooks.db")
    monkeypatch.setenv("WEBHOOK_DB_PATH", db)
    # Patch the module-level helper used inside storage.py
    monkeypatch.setattr(_storage_mod, "_db_path", lambda: db)
    return db


def test_store_invoice_create_event(tmp_db):
    init_db()
    count = store_webhook_payload(SAMPLE_PAYLOAD, json.dumps(SAMPLE_PAYLOAD))
    assert count == 2   # two entity changes in SAMPLE_PAYLOAD


def test_count_all_events(tmp_db):
    init_db()
    store_webhook_payload(SAMPLE_PAYLOAD, json.dumps(SAMPLE_PAYLOAD))
    assert count_all_events() == 2


def test_store_cloudevents_payload(tmp_db):
    init_db()
    count = store_webhook_payload(
        CLOUDEVENTS_PAYLOAD,
        json.dumps(CLOUDEVENTS_PAYLOAD),
    )
    assert count == 1
    assert count_all_events() == 1


def test_empty_payload_stores_zero(tmp_db):
    init_db()
    count = store_webhook_payload({"eventNotifications": []}, "{}")
    assert count == 0


def test_db_does_not_exist_returns_zero(tmp_path, monkeypatch):
    missing = str(tmp_path / "does_not_exist.db")
    monkeypatch.setattr(_storage_mod, "_db_path", lambda: missing)
    assert count_all_events() == 0


# ── FastAPI endpoint ──────────────────────────────────────────────────────────

@pytest.fixture()
def web_client(tmp_db, monkeypatch):
    """TestClient with verifier token and temp DB configured."""
    monkeypatch.setenv("QBO_WEBHOOK_VERIFIER_TOKEN", TOKEN)
    monkeypatch.setenv("QBO_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("QBO_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("QBO_REDIRECT_URI", "https://example.com/oauth/callback")
    monkeypatch.setenv("QBO_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("QBO_REALM_ID", "123456")
    from webhook.server import app
    return TestClient(app, raise_server_exceptions=True)


def test_endpoint_accepts_cloudevents_request(web_client):
    sig, _ = compute_signature(CLOUDEVENTS_BYTES, TOKEN)
    resp = web_client.post(
        "/webhook",
        content=CLOUDEVENTS_BYTES,
        headers={"Content-Type": "application/json", "intuit-signature": sig},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["events_stored"] == 1
    assert body["invoices_queued"] == 1


def test_endpoint_accepts_valid_request(web_client):
    sig, _ = compute_signature(SAMPLE_BYTES, TOKEN)
    resp = web_client.post(
        "/webhook",
        content=SAMPLE_BYTES,
        headers={"Content-Type": "application/json", "intuit-signature": sig},
    )
    assert resp.status_code == 200
    assert resp.json()["events_stored"] == 2


def test_endpoint_rejects_bad_signature(web_client):
    resp = web_client.post(
        "/webhook",
        content=SAMPLE_BYTES,
        headers={"Content-Type": "application/json", "intuit-signature": "bad"},
    )
    assert resp.status_code == 401


def test_endpoint_rejects_missing_signature(web_client):
    resp = web_client.post(
        "/webhook",
        content=SAMPLE_BYTES,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_health_endpoint(web_client, monkeypatch):
    class _FakeQBO:
        def get_company_info(self):
            return {"CompanyName": "Test Co"}

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())
    resp = web_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["qbo_webhook_verifier_configured"] is True
    assert body["qbo_api_ok"] is True


def test_oauth_callback_success_page(web_client):
    resp = web_client.get("/oauth/callback?code=abc&realmId=123&state=xyz")
    assert resp.status_code == 200
    assert "authorization complete" in resp.text.lower()
    assert "realmId" in resp.text


def test_oauth_callback_error_page(web_client):
    resp = web_client.get("/oauth/callback?error=access_denied")
    assert resp.status_code == 200
    assert "authorization failed" in resp.text.lower()


def test_payment_success_page_without_id_is_unknown(web_client):
    """Bare /payment/success must NOT claim success (no verified status)."""
    resp = web_client.get("/payment/success")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Payment successful" not in resp.text
    assert "confirming your payment" in resp.text.lower()


def test_payment_success_page_canceled_charge_not_success(web_client, monkeypatch):
    """Canceled / not-captured charges must never show Payment successful."""

    class _FakeUP:
        def get_charge_status(self, charge_id: str):
            assert charge_id == "track_canceled_1"
            return {
                "status": True,
                "data": {
                    "transaction": {
                        "result": "CANCELED",
                        "track_id": charge_id,
                    }
                },
            }

    monkeypatch.setattr(
        "upayments.client.upayments_client_from_env",
        lambda **kw: _FakeUP(),
    )

    resp = web_client.get("/payment/success?track_id=track_canceled_1")
    assert resp.status_code == 200
    assert "Payment successful" not in resp.text
    assert "Payment not completed" in resp.text
    assert "wa.me/96565068000" in resp.text


def test_payment_success_page_not_captured_not_success(web_client, monkeypatch):
    class _FakeUP:
        def get_charge_status(self, charge_id: str):
            return {"data": {"transaction": {"result": "NOT CAPTURED"}}}

    monkeypatch.setattr(
        "upayments.client.upayments_client_from_env",
        lambda **kw: _FakeUP(),
    )
    resp = web_client.get("/payment/success?track_id=track_fail")
    assert resp.status_code == 200
    assert "Payment successful" not in resp.text
    assert "Payment not completed" in resp.text


def test_payment_success_page_captured_shows_success(web_client, monkeypatch):
    class _FakeUP:
        def get_charge_status(self, charge_id: str):
            return {"data": {"transaction": {"result": "CAPTURED"}}}

    monkeypatch.setattr(
        "upayments.client.upayments_client_from_env",
        lambda **kw: _FakeUP(),
    )
    resp = web_client.get("/payment/success?track_id=track_ok")
    assert resp.status_code == 200
    assert "Payment successful" in resp.text


def test_payment_success_page_pending_is_unknown(web_client, monkeypatch):
    class _FakeUP:
        def get_charge_status(self, charge_id: str):
            return {"data": {"transaction": {"result": "PENDING"}}}

    monkeypatch.setattr(
        "upayments.client.upayments_client_from_env",
        lambda **kw: _FakeUP(),
    )
    resp = web_client.get("/payment/success?track_id=track_pending")
    assert resp.status_code == 200
    assert "Payment successful" not in resp.text
    assert "Payment not completed" not in resp.text
    assert "confirming your payment" in resp.text.lower()


def test_payment_success_page_api_error_is_unknown(web_client, monkeypatch):
    from upayments.exceptions import UPaymentsAPIError

    class _FakeUP:
        def get_charge_status(self, charge_id: str):
            raise UPaymentsAPIError("upstream down", status_code=503)

    monkeypatch.setattr(
        "upayments.client.upayments_client_from_env",
        lambda **kw: _FakeUP(),
    )
    resp = web_client.get("/payment/success?session_id=sess_x")
    assert resp.status_code == 200
    assert "Payment successful" not in resp.text
    assert "confirming your payment" in resp.text.lower()


def test_invoice_pdf_endpoint_returns_pdf(web_client, monkeypatch):
    pdf_bytes = b"%PDF-1.4 test"

    class _FakeQBO:
        def get_invoice_pdf(self, invoice_id: str) -> bytes:
            assert invoice_id == "9258"
            return pdf_bytes

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())

    resp = web_client.get("/invoice/9258/pdf")
    assert resp.status_code == 200
    assert resp.content == pdf_bytes
    assert resp.headers["content-type"] == "application/pdf"
    assert 'filename="invoice-9258.pdf"' in resp.headers["content-disposition"]


def test_invoice_pdf_endpoint_returns_404_on_qbo_error(web_client, monkeypatch):
    from qbo.client import QBOError

    class _FakeQBO:
        def get_invoice_pdf(self, invoice_id: str) -> bytes:
            raise QBOError("QuickBooks PDF error 404 for invoice missing: Not found")

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())

    resp = web_client.get("/invoice/missing/pdf")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invoice PDF not found"


def test_invoice_pdf_endpoint_returns_503_on_auth_error(web_client, monkeypatch):
    from qbo.client import NotAuthorizedError

    class _FakeQBO:
        def get_invoice_pdf(self, invoice_id: str) -> bytes:
            raise NotAuthorizedError("QuickBooks token refresh failed")

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())

    resp = web_client.get("/invoice/9286/pdf")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "QuickBooks authorization required"


def test_whatsapp_webhook_rejects_non_admin(web_client, monkeypatch):
    monkeypatch.setenv("TWILIO_ADMIN_PHONE", "+96550655856")
    resp = web_client.post(
        "/webhook/whatsapp",
        content="From=whatsapp%3A%2B96599999999&Body=PAID_2537",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 403


def test_whatsapp_webhook_handles_paid_action(web_client, monkeypatch):
    monkeypatch.setenv("TWILIO_ADMIN_PHONE", "+96550655856")

    class _FakeQBO:
        def find_invoice_by_doc_number(self, doc_number):
            return {
                "Id": "42", "DocNumber": doc_number,
                "TotalAmt": 160.0, "Balance": 160.0,
                "CustomerRef": {"value": "99", "name": "Coded"},
            }

        def create_bank_transfer_payment(self, **kwargs):
            return {"Id": "PAY-99"}

    class _FakeWA:
        def send_text(self, to_number, body):
            return MagicMock(sent=True, sid="SM_reply")

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())
    monkeypatch.setattr(
        "messaging.whatsapp.whatsapp_client_from_settings",
        lambda settings=None: _FakeWA(),
    )
    monkeypatch.setattr("db.payment_links.get_by_invoice_id", lambda _: None)
    monkeypatch.setattr("db.payment_links.mark_payment_captured", lambda *a, **k: None)

    resp = web_client.post(
        "/webhook/whatsapp",
        content="From=whatsapp%3A%2B96550655856&Body=PAID_2537",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["action"] == "PAID"
    assert "PAY-99" in body["reply"]


def test_tap_webhook_unknown_charge_after_startup(web_client, tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    class _FakeQBO:
        pass

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())

    from webhook.server import app
    client = TestClient(app, raise_server_exceptions=True)

    resp = client.post(
        "/webhook/tap",
        json={
            "id": "chg_test_unknown",
            "status": "CAPTURED",
            "amount": 1,
            "currency": "KWD",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "unknown_charge"


_UPAYMENTS_FORM = (
    "payment_id=101620726000342223"
    "&result=CAPTURED"
    "&post_date=0726"
    "&tran_id=620771009962568"
    "&ref=620771030910"
    "&track_id=019f9eb3ad23e23a5e8744219008d63bv2"
    "&auth=337456"
    "&order_id=019f9eb3ad23e23a5e8744219008d63a"
    "&requested_order_id=LIVE-TEST-001"
    "&refund_order_id=019f9eb3ad23e23a5e8744219008d63a"
    "&invoice_id=39686284"
    "&payment_type=knet"
    "&payment_method=knet"
    "&transaction_date=2026-07-26+16%3A54%3A03"
    "&receipt_id=019f9eb3ad23e23a5e8744219008d63a"
    "&trn_udf=merchant_id%3D78508%3Binvoice%3DLIVE-TEST-001"
)


def test_upayments_webhook_rejects_bad_token(web_client, monkeypatch):
    monkeypatch.setenv("UPAYMENTS_WEBHOOK_SECRET", "expected-token")
    resp = web_client.post(
        "/webhook/upayments",
        content=_UPAYMENTS_FORM,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-notification-token": "wrong-token",
        },
    )
    assert resp.status_code == 401


def test_upayments_webhook_unknown_charge(web_client, tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("UPAYMENTS_WEBHOOK_SECRET", "expected-token")

    class _FakeQBO:
        pass

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())
    monkeypatch.setattr(
        "messaging.whatsapp.whatsapp_client_from_settings",
        lambda settings=None: None,
    )

    resp = web_client.post(
        "/webhook/upayments",
        content=_UPAYMENTS_FORM,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-notification-token": "expected-token",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unknown_charge"
    assert body["invoice_number"] == "LIVE-TEST-001"


def test_upayments_webhook_captures_by_invoice_number(web_client, tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("UPAYMENTS_WEBHOOK_SECRET", "expected-token")

    from db import payment_links as db

    db.init_table()
    db.create_link(
        invoice_id="inv-99",
        customer_id="cust-1",
        tap_charge_id="session-abc",
        payment_url="https://pay.example/abc",
        amount=1.0,
        invoice_number="LIVE-TEST-001",
        customer_name="Test Co",
        currency="KWD",
    )

    class _FakeQBO:
        def create_payment(self, **kwargs):
            return {"Id": "PAY-UP-1"}

        def get_customer_by_id(self, customer_id):
            from qbo.models import Customer
            return Customer(
                id=customer_id,
                display_name="Test Co",
                email="",
                phone="+96550000000",
                mobile="",
                alternate_phone="",
            )

    class _FakeWA:
        def send_payment_confirmation(self, *a, **k):
            return MagicMock(sent=True, sid="SM1")

        def send_admin_payment_received_notify(self, *a, **k):
            return MagicMock(sent=True, sid="SM-ADMIN-1")

    monkeypatch.setattr("qbo.client.QuickBooksClient", lambda settings=None: _FakeQBO())
    monkeypatch.setattr(
        "messaging.whatsapp.whatsapp_client_from_settings",
        lambda settings=None: _FakeWA(),
    )

    resp = web_client.post(
        "/webhook/upayments",
        content=_UPAYMENTS_FORM,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-notification-token": "expected-token",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["capture_status"] == "PAYMENT_CAPTURED"
    assert body["invoice_id"] == "inv-99"
    assert body["qbo_payment_id"] == "PAY-UP-1"


def test_upayments_webhook_ignores_non_captured(web_client, monkeypatch):
    monkeypatch.setenv("UPAYMENTS_WEBHOOK_SECRET", "expected-token")
    resp = web_client.post(
        "/webhook/upayments",
        content="result=NOT+CAPTURED&track_id=t1&requested_order_id=2555",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "x-notification-token": "expected-token",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


# ── webhook delivery counters ────────────────────────────────────────────────

def test_delivery_counters_distinguish_rejected_from_never_called(web_client):
    """A 401'd webhook must still be visible — it stores no event."""
    from webhook.server import _QBO_WEBHOOK_STATS

    _QBO_WEBHOOK_STATS.update(
        last_request_at=None, accepted=0, rejected_signature=0, last_rejected_at=None
    )

    # Nothing has called us yet.
    assert _QBO_WEBHOOK_STATS["last_request_at"] is None

    web_client.post(
        "/webhook",
        content=SAMPLE_BYTES,
        headers={"Content-Type": "application/json", "intuit-signature": "bad"},
    )
    # Rejected, so no event stored — but the attempt is now recorded.
    assert _QBO_WEBHOOK_STATS["rejected_signature"] == 1
    assert _QBO_WEBHOOK_STATS["accepted"] == 0
    assert _QBO_WEBHOOK_STATS["last_request_at"] is not None
    assert _QBO_WEBHOOK_STATS["last_rejected_at"] is not None

    sig, _ = compute_signature(SAMPLE_BYTES, TOKEN)
    web_client.post(
        "/webhook",
        content=SAMPLE_BYTES,
        headers={"Content-Type": "application/json", "intuit-signature": sig},
    )
    assert _QBO_WEBHOOK_STATS["accepted"] == 1
    assert _QBO_WEBHOOK_STATS["rejected_signature"] == 1


def test_health_exposes_delivery_counters(web_client):
    body = web_client.get("/health").json()
    assert "qbo_webhook_delivery" in body
    assert set(body["qbo_webhook_delivery"]) == {
        "last_request_at",
        "accepted",
        "rejected_signature",
        "last_rejected_at",
    }
