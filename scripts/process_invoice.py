"""Generate UPayments link + WhatsApp for a QBO invoice by doc number.

Use when the QBO webhook was missed (e.g. invoice created before automation was fixed).

    python -m scripts.process_invoice 2554
"""
from __future__ import annotations

import sys

from config import get_settings
from db.payment_links import init_table
from messaging.whatsapp import whatsapp_client_from_settings
from qbo.client import QuickBooksClient
from upayments.client import upayments_client_from_env
from workflows.invoice_to_tap import LinkResult, SkipResult, process_invoice


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.process_invoice <invoice_number>")
        raise SystemExit(1)

    doc_number = sys.argv[1].strip().lstrip("#")
    init_table()

    settings = get_settings()
    qbo = QuickBooksClient(settings=settings)
    invoice = qbo.find_invoice_by_doc_number(doc_number)
    if invoice is None:
        print(f"Invoice #{doc_number} not found in QuickBooks.")
        raise SystemExit(1)

    invoice_id = str(invoice.get("Id", ""))
    balance = float(invoice.get("Balance", 0))
    if balance <= 0:
        print(f"Invoice #{doc_number} has no open balance — nothing to send.")
        raise SystemExit(1)

    upayments = upayments_client_from_env()
    whatsapp = whatsapp_client_from_settings(settings)
    result = process_invoice(
        invoice_id,
        qbo_client=qbo,
        upayments_client=upayments,
        whatsapp_client=whatsapp,
        settings=settings,
    )

    if isinstance(result, SkipResult):
        print(f"Skipped: {result.reason}")
        if result.detail:
            print(result.detail)
        raise SystemExit(1)

    if isinstance(result, LinkResult):
        label = "Existing link returned" if result.status == "EXISTING_LINK_RETURNED" else "Link generated"
        print(f"{label} for invoice #{result.invoice_number}")
        print(f"  Payment URL: {result.payment_url}")
        print(f"  Provider ID: {result.tap_charge_id}")
        print(f"  WhatsApp:    {'sent to ' + result.whatsapp_number if result.whatsapp_sent else 'not sent'}")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
