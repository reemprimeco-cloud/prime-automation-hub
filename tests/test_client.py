"""Offline tests for the client's query building, response parsing, and Customer model.

These run WITHOUT live QuickBooks credentials by mocking the HTTP layer.
Run with:  python -m pytest -q
"""
from __future__ import annotations

import time

from config import Settings
from auth.token_store import TokenData
from qbo.models import Customer
import qbo.client as qbo_client


# ── fixtures ────────────────────────────────────────────────────────────────

def _settings() -> Settings:
    return Settings(
        client_id="id",
        client_secret="secret",
        redirect_uri="http://localhost:8000/callback",
        environment="production",
        minor_version="75",
        token_path="tokens.json",
        realm_id=None,
        webhook_verifier_token=None,
    )


def _fresh_tokens() -> TokenData:
    now = time.time()
    return TokenData(
        access_token="access",
        refresh_token="refresh",
        realm_id="123456789",
        access_token_expires_at=now + 3600,
        refresh_token_expires_at=now + 8_640_000,
    )


def _make_client(monkeypatch, response_payload):
    monkeypatch.setattr(qbo_client, "load_tokens", lambda _p: _fresh_tokens())
    client = qbo_client.QuickBooksClient(settings=_settings())
    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return response_payload

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(client._session, "request", fake_request)
    return client, captured


# ── Customer model ───────────────────────────────────────────────────────────

def test_customer_model_maps_all_fields():
    raw = {
        "Id": "42",
        "DisplayName": "Alice's Hardware",
        "PrimaryEmailAddr": {"Address": "alice@example.com"},
        "PrimaryPhone":     {"FreeFormNumber": "415-555-1234"},
        "Mobile":           {"FreeFormNumber": "415-555-5678"},
        "AlternatePhone":   {"FreeFormNumber": "415-555-9999"},
    }
    c = Customer.from_qbo(raw)
    assert c.id             == "42"
    assert c.display_name   == "Alice's Hardware"
    assert c.email          == "alice@example.com"
    assert c.phone          == "415-555-1234"
    assert c.mobile         == "415-555-5678"
    assert c.alternate_phone == "415-555-9999"


def test_customer_model_handles_missing_optional_fields():
    """Phone, mobile, and alternate_phone are optional in QBO — missing keys must not raise."""
    raw = {"Id": "7", "DisplayName": "Bob"}
    c = Customer.from_qbo(raw)
    assert c.id             == "7"
    assert c.display_name   == "Bob"
    assert c.email          == ""
    assert c.phone          == ""
    assert c.mobile         == ""
    assert c.alternate_phone == ""


def test_customer_model_handles_null_nested_objects():
    """QBO sometimes sends explicit null for empty nested objects."""
    raw = {
        "Id": "8",
        "DisplayName": "Carol",
        "PrimaryEmailAddr": None,
        "PrimaryPhone": None,
        "Mobile": None,
    }
    c = Customer.from_qbo(raw)
    assert c.email  == ""
    assert c.phone  == ""
    assert c.mobile == ""


# ── get_customer_by_id ───────────────────────────────────────────────────────

def test_get_customer_by_id_uses_direct_endpoint(monkeypatch):
    """Must call the direct /customer/{id} REST endpoint — no SQL query, no name matching."""
    payload = {
        "Customer": {
            "Id": "42",
            "DisplayName": "Alice's Hardware",
            "PrimaryEmailAddr": {"Address": "alice@example.com"},
            "PrimaryPhone":     {"FreeFormNumber": "415-555-1234"},
            "Mobile":           {"FreeFormNumber": "415-555-5678"},
        }
    }
    client, captured = _make_client(monkeypatch, payload)
    c = client.get_customer_by_id("42")

    # Endpoint must contain the customer ID — not a /query path
    assert captured["url"].endswith("/v3/company/123456789/customer/42")
    assert "query" not in (captured.get("params") or {})

    # All five required fields must be populated correctly
    assert c.id           == "42"
    assert c.display_name == "Alice's Hardware"
    assert c.email        == "alice@example.com"
    assert c.phone        == "415-555-1234"
    assert c.mobile       == "415-555-5678"


def test_get_customer_by_id_reads_notes(monkeypatch):
    payload = {
        "Customer": {
            "Id": "42",
            "DisplayName": "Alice's Hardware",
            "Notes": "BANK_TRANSFER",
        }
    }
    client, _ = _make_client(monkeypatch, payload)
    c = client.get_customer_by_id("42")
    assert c.notes == "BANK_TRANSFER"


def test_customer_from_qbo_reads_notes():
    raw = {"Id": "1", "DisplayName": "Test Co", "Notes": "  bank transfer preferred  "}
    c = Customer.from_qbo(raw)
    assert c.notes == "bank transfer preferred"


def test_get_customer_by_id_accepts_integer(monkeypatch):
    """customer_id may be passed as an int (common when reading from a DB)."""
    payload = {"Customer": {"Id": "99", "DisplayName": "Dave"}}
    client, captured = _make_client(monkeypatch, payload)
    c = client.get_customer_by_id(99)
    assert captured["url"].endswith("/customer/99")
    assert c.id == "99"


# ── get_customers (list) ─────────────────────────────────────────────────────

def test_get_customers_returns_customer_objects(monkeypatch):
    payload = {"QueryResponse": {"Customer": [
        {"Id": "1", "DisplayName": "Acme",
         "PrimaryEmailAddr": {"Address": "acme@corp.com"},
         "PrimaryPhone": {"FreeFormNumber": "212-555-0001"},
         "Mobile": {"FreeFormNumber": ""}},
    ]}}
    client, captured = _make_client(monkeypatch, payload)
    customers = client.get_customers(max_results=50)

    assert len(customers) == 1
    assert isinstance(customers[0], Customer)
    assert customers[0].id    == "1"
    assert customers[0].email == "acme@corp.com"
    assert customers[0].phone == "212-555-0001"
    assert captured["params"]["query"] == "SELECT * FROM Customer MAXRESULTS 50"


def test_get_customers_returns_empty_list(monkeypatch):
    client, _ = _make_client(monkeypatch, {"QueryResponse": {}})
    assert client.get_customers() == []


# ── existing tests ────────────────────────────────────────────────────────────

def test_get_invoices_builds_query_and_parses(monkeypatch):
    payload = {"QueryResponse": {"Invoice": [{"DocNumber": "1001", "TotalAmt": 50.0}]}}
    client, captured = _make_client(monkeypatch, payload)
    invoices = client.get_invoices(max_results=10)
    assert invoices == [{"DocNumber": "1001", "TotalAmt": 50.0}]
    assert captured["url"].endswith("/v3/company/123456789/query")
    assert captured["params"]["query"] == "SELECT * FROM Invoice ORDERBY TxnDate DESC MAXRESULTS 10"
    assert captured["params"]["minorversion"] == "75"


def test_company_info_unwraps_envelope(monkeypatch):
    payload = {"CompanyInfo": {"CompanyName": "Acme"}}
    client, captured = _make_client(monkeypatch, payload)
    info = client.get_company_info()
    assert info == {"CompanyName": "Acme"}
    assert captured["url"].endswith("/v3/company/123456789/companyinfo/123456789")


def test_get_preferences_unwraps_envelope(monkeypatch):
    payload = {
        "Preferences": {
            "SalesFormsPrefs": {
                "CustomField": [
                    {"CustomFieldDefinitionId": "1", "Name": "Rep", "Type": "StringType", "Active": True}
                ]
            }
        }
    }
    client, captured = _make_client(monkeypatch, payload)
    prefs = client.get_preferences()
    assert captured["url"].endswith("/v3/company/123456789/preferences")
    cfs = prefs["SalesFormsPrefs"]["CustomField"]
    assert len(cfs) == 1
    assert cfs[0]["Name"] == "Rep"


def test_create_payment_posts_linked_payment(monkeypatch):
    payload = {
        "Payment": {
            "Id": "PAY-99",
            "TotalAmt": 48.0,
            "CustomerRef": {"value": "99"},
        }
    }
    client, captured = _make_client(monkeypatch, payload)

    payment = client.create_payment(
        customer_id="99",
        invoice_id="9258",
        amount=48.0,
        tap_charge_id="chg_test",
    )

    assert payment["Id"] == "PAY-99"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v3/company/123456789/payment")
    body = captured.get("json") or {}
    assert body["CustomerRef"]["value"] == "99"
    assert body["TotalAmt"] == 48.0
    assert body["Line"][0]["LinkedTxn"][0]["TxnId"] == "9258"
    assert body["PaymentMethodRef"] == {"value": "1000000001"}
    assert body["DepositToAccountRef"] == {"value": "29"}
    assert "chg_test" in body["PrivateNote"]


def test_get_invoice_pdf_fetches_binary(monkeypatch):
    pdf_bytes = b"%PDF-1.4 fake invoice pdf"
    monkeypatch.setattr(qbo_client, "load_tokens", lambda _p: _fresh_tokens())
    client = qbo_client.QuickBooksClient(settings=_settings())
    captured = {}

    class _Resp:
        status_code = 200

        @property
        def content(self):
            return pdf_bytes

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(client._session, "get", fake_get)
    result = client.get_invoice_pdf("9258")

    assert result == pdf_bytes
    assert captured["url"].endswith("/v3/company/123456789/invoice/9258/pdf")
    assert captured["headers"]["Accept"] == "application/pdf"
    assert captured["params"]["minorversion"] == "75"


def test_get_invoice_pdf_raises_on_error(monkeypatch):
    monkeypatch.setattr(qbo_client, "load_tokens", lambda _p: _fresh_tokens())
    client = qbo_client.QuickBooksClient(settings=_settings())

    class _Resp:
        status_code = 404
        text = "Not found"

        @property
        def content(self):
            return b""

    monkeypatch.setattr(client._session, "get", lambda *a, **k: _Resp())

    import pytest
    from qbo.client import QBOError

    with pytest.raises(QBOError, match="PDF error 404"):
        client.get_invoice_pdf("missing")
