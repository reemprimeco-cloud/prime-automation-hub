"""Phase 5 test — simulate a Tap charge.captured payment.

Looks up an existing payment link in the DB and runs the full capture workflow:
  → Creates QBO payment (invoice goes Paid ✓)
  → Updates DB status to PAYMENT_CAPTURED
  → Sends WhatsApp confirmation to customer

Usage
-----
    # Use the charge ID from your DB or from a previous Phase 3 test:
    python -m scripts.test_payment_capture chg_LV07H1820261050Bq2e0806575

    # Or list all payment links in the DB:
    python -m scripts.test_payment_capture --list
"""
from __future__ import annotations

import sys

from config import get_settings
from db import payment_links as db
from db.payment_links import init_table
from messaging.whatsapp import whatsapp_client_from_settings
from qbo.client import QuickBooksClient
from workflows.payment_capture import CaptureError, CaptureResult, handle_payment_capture

W = 66


def _hr(c="─"): print(c * W)
def _ok(label, val=""): print(f"  ✓  {label:<28} {val}")
def _fail(label, val=""): print(f"  ✗  {label:<28} {val}")


def _list_links() -> None:
    """Print all payment links currently in the database."""
    init_table()
    from db.connection import get_connection
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT invoice_id, invoice_number, customer_name, "
            "tap_charge_id, amount, currency, status "
            "FROM payment_links ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        print("No payment links in the database yet.")
        print("Run:  python -m scripts.test_invoice_to_tap  first.")
        return

    print(f"\n  {'Invoice ID':<10} {'Doc#':<8} {'Customer':<22} {'Tap Charge ID':<36} {'Amount':>8}  Status")
    _hr()
    for r in rows:
        print(
            f"  {r[0]:<10} {(r[1] or ''):<8} {(r[2] or '')[:21]:<22} "
            f"{(r[3] or ''):<36} {r[4]:>8.3f}  {r[5]}"
        )
    print()


def main() -> None:
    args = sys.argv[1:]

    if not args or "--list" in args:
        _list_links()
        return

    tap_charge_id = args[0]

    settings = get_settings()
    qbo      = QuickBooksClient(settings=settings)
    wa       = whatsapp_client_from_settings(settings)

    print()
    _hr("═")
    print("  Phase 5 Test — Payment Capture")
    _hr("═")
    print(f"  Charge ID: {tap_charge_id}")

    # Look up the record first to show context
    init_table()
    record = db.get_by_charge_id(tap_charge_id)
    if record:
        print(f"  Invoice:   #{record.get('invoice_number')} (ID: {record.get('invoice_id')})")
        print(f"  Customer:  {record.get('customer_name')}")
        print(f"  Amount:    {record.get('amount'):.3f} {record.get('currency')}")
        print(f"  Status:    {record.get('status')}")
    else:
        print("  ⚠  Charge not found in DB. Run Phase 3 test first.")
        _hr("═")
        return

    print()
    _hr()
    print("  Running capture workflow...")
    _hr()

    result = handle_payment_capture(
        tap_charge_id,
        float(record.get("amount", 0)),
        record.get("currency", "KWD"),
        qbo_client=qbo,
        whatsapp_client=wa,
    )

    print()
    _hr("═")

    if isinstance(result, CaptureError):
        _fail("Capture failed", result.reason)
        _fail("Detail", result.detail[:50])
        _hr("═")
        sys.exit(1)

    if isinstance(result, CaptureResult):
        label = "Already captured" if result.status == "ALREADY_CAPTURED" else "Payment captured"
        _ok("Status", label)
        _ok("Invoice ID", result.invoice_id)
        _ok("Invoice Number", f"#{result.invoice_number}")
        _ok("Customer", result.customer_name)
        _ok("Amount", f"{result.amount:.3f} {result.currency}")
        _ok("QBO Payment ID", result.qbo_payment_id or "—")
        _ok("QBO Invoice", "Marked PAID ✓" if result.qbo_payment_created else "—")
        if result.whatsapp_sent:
            _ok("WhatsApp confirmation", result.whatsapp_number)
        elif result.whatsapp_number:
            _fail("WhatsApp failed", "check logs")
        else:
            print(f"  ⚠   {'WhatsApp skipped':<28} no phone on record")
        _hr("═")
        print()
        print("  Open QuickBooks — invoice should show PAID ✓")
        print()


if __name__ == "__main__":
    main()
