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

import pytest
from fastapi.testclient import TestClient

from webhook.verify import compute_signature, verify_signature
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


# ── signature verification ────────────────────────────────────────────────────

def test_valid_signature_accepted():
    sig = compute_signature(SAMPLE_BYTES, TOKEN)
    assert verify_signature(SAMPLE_BYTES, sig, TOKEN) is True


def test_wrong_token_rejected():
    sig = compute_signature(SAMPLE_BYTES, TOKEN)
    assert verify_signature(SAMPLE_BYTES, sig, "wrong-token") is False


def test_tampered_payload_rejected():
    sig = compute_signature(SAMPLE_BYTES, TOKEN)
    tampered = SAMPLE_BYTES + b" "
    assert verify_signature(tampered, sig, TOKEN) is False


def test_empty_signature_rejected():
    assert verify_signature(SAMPLE_BYTES, "", TOKEN) is False


def test_empty_token_rejected():
    sig = compute_signature(SAMPLE_BYTES, TOKEN)
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
    from webhook.server import app
    return TestClient(app, raise_server_exceptions=True)


def test_endpoint_accepts_valid_request(web_client):
    sig = compute_signature(SAMPLE_BYTES, TOKEN)
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


def test_health_endpoint(web_client):
    resp = web_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["qbo_token_configured"] is True


def test_payment_success_page(web_client):
    resp = web_client.get("/payment/success")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Payment successful" in resp.text
