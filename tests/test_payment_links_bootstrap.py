"""Tests for registering payment links created outside Render."""
from __future__ import annotations

import json
from pathlib import Path

from db import payment_links as db


def test_register_existing_link_is_idempotent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    kwargs = dict(
        invoice_id="9278",
        customer_id="1286",
        tap_charge_id="chg_test_register",
        payment_url="https://checkout.tap.company/test",
        amount=10.0,
        invoice_number="2533",
        customer_name="Test Customer",
        whatsapp_to_number="+96550000000",
    )

    assert db.register_existing_link(**kwargs) is True
    assert db.register_existing_link(**kwargs) is False

    row = db.get_by_charge_id("chg_test_register")
    assert row is not None
    assert row["invoice_id"] == "9278"
    assert row["whatsapp_to_number"] == "+96550000000"


def test_bootstrap_links_from_file(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    bootstrap = tmp_path / "links.json"
    bootstrap.write_text(
        json.dumps([
            {
                "invoice_id": "9001",
                "customer_id": "100",
                "tap_charge_id": "chg_bootstrap",
                "payment_url": "https://example.com/pay",
                "amount": 5.0,
                "invoice_number": "9001",
                "customer_name": "Bootstrap Customer",
            }
        ]),
        encoding="utf-8",
    )

    count = db.bootstrap_links_from_file(str(bootstrap))
    assert count == 1
    assert db.get_by_invoice_id("9001") is not None
    assert db.bootstrap_links_from_file(str(bootstrap)) == 0


def test_repo_bootstrap_file_has_legacy_invoices():
    path = Path(__file__).resolve().parent.parent / "data" / "bootstrap_payment_links.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    ids = {rec["invoice_number"] for rec in records}
    assert "2533" in ids
    assert "2534" in ids
