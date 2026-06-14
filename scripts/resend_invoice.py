"""Resend the customer payment WhatsApp for a QBO invoice by doc number.

    python -m scripts.resend_invoice 2551
"""
from __future__ import annotations

import sys

from config import get_settings
from db.payment_links import init_table
from messaging.whatsapp import whatsapp_client_from_settings
from qbo.client import QuickBooksClient
from workflows.admin_whatsapp import handle_admin_action


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.resend_invoice <invoice_number>")
        raise SystemExit(1)

    doc_number = sys.argv[1].strip().lstrip("#")
    init_table()

    settings = get_settings()
    qbo = QuickBooksClient(settings=settings)
    whatsapp = whatsapp_client_from_settings(settings)

    result = handle_admin_action(
        "RESEND",
        doc_number,
        qbo_client=qbo,
        whatsapp_client=whatsapp,
        settings=settings,
    )
    print(result.reply)
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
