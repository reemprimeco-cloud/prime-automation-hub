"""Phase 3 test script — Invoice → Tap Payment Link → QuickBooks Updated.

Usage
-----
    python -m scripts.test_invoice_to_tap                # auto-picks latest open invoice
    python -m scripts.test_invoice_to_tap 42             # uses invoice ID 42
    python -m scripts.test_invoice_to_tap --list         # lists open invoices, no action
    python -m scripts.test_invoice_to_tap 42 --dry-run   # shows what would be sent, skips Tap

Requirements
------------
* .env must have QBO_* credentials and TAP_SECRET_KEY
* Run python -m scripts.authorize first if tokens.json doesn't exist
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from config import get_settings
from db import payment_links as db
from qbo.client import QuickBooksClient
from tap.client import tap_client_from_settings
from workflows.invoice_to_tap import LinkResult, SkipResult, process_invoice


# ── formatting helpers ────────────────────────────────────────────────────────

W = 66   # console width

def _hr(char="─"):       print(char * W)
def _line(t=""):         print(f"  {t}")
def _ok(label, val=""):  print(f"  ✓  {label:<28} {val}")
def _fail(label, val=""): print(f"  ✗  {label:<28} {val}")
def _step(n, total, label):
    print(f"\n  [{n}/{total}] {label}", end="", flush=True)
def _done(msg="done"):
    print(f"  → {msg}")


# ── invoice discovery ─────────────────────────────────────────────────────────

def _list_open_invoices(client: QuickBooksClient, limit: int = 10) -> list[dict]:
    """Return up to `limit` open (Balance > 0) invoices, newest first."""
    data = client.query(
        f"SELECT * FROM Invoice WHERE Balance > '0' "
        f"ORDERBY TxnDate DESC MAXRESULTS {limit}"
    )
    return (data.get("QueryResponse") or {}).get("Invoice") or []


# ── dry-run display ───────────────────────────────────────────────────────────

def _dry_run_display(invoice: dict, customer, settings) -> None:
    from workflows.invoice_to_tap import _split_name, _phone_for_tap
    first, last = _split_name(customer.display_name)
    phone = _phone_for_tap(customer)
    print()
    _hr("═")
    _line("DRY RUN — Tap request that would be sent:")
    _hr()
    _line(f"  Endpoint   POST https://api.tap.company/v2/charges")
    _line(f"  amount     {float(invoice.get('TotalAmt', 0)):.3f} KWD")
    _line(f"  customer   {first} {last}")
    _line(f"  phone      +{phone.country_code}{phone.number}" if phone.number else "  phone     (none)")
    _line(f"  email      {customer.email or '(none)'}")
    _line(f"  reference  INV-{invoice.get('DocNumber')} / order: {invoice.get('Id')}")
    _line(f"  redirect   {settings.tap_redirect_url}")
    _line(f"  webhook    {settings.tap_webhook_url}")
    _hr("═")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    dry_run  = "--dry-run" in args
    list_only = "--list" in args
    invoice_id_arg = next((a for a in args if not a.startswith("--")), None)

    settings = get_settings()
    qbo = QuickBooksClient(settings=settings)

    # ── --list mode ───────────────────────────────────────────────────────────
    if list_only:
        invoices = _list_open_invoices(qbo)
        if not invoices:
            print("No open invoices found.")
            return
        print(f"\n  {'ID':<8} {'Doc #':<10} {'Date':<12} {'Customer':<28} {'Balance':>10}")
        _hr()
        for inv in invoices:
            cref = inv.get("CustomerRef") or {}
            print(
                f"  {inv['Id']:<8} {inv.get('DocNumber',''):<10} "
                f"{inv.get('TxnDate',''):<12} {cref.get('name','')[:27]:<28} "
                f"{float(inv.get('Balance',0)):>10.3f}"
            )
        print()
        return

    # ── resolve invoice ID ────────────────────────────────────────────────────
    if invoice_id_arg:
        invoice_id = invoice_id_arg
    else:
        invoices = _list_open_invoices(qbo, limit=1)
        if not invoices:
            print("No open invoices found. Create an invoice in QuickBooks first.")
            sys.exit(1)
        invoice_id = invoices[0]["Id"]
        print(f"\n  Auto-selected latest open invoice: ID {invoice_id}")

    # ── header ────────────────────────────────────────────────────────────────
    print()
    _hr("═")
    print(f"  Phase 3 Test — Invoice → Tap Payment Link")
    if dry_run:
        print("  [DRY RUN MODE — Tap API will NOT be called]")
    _hr("═")

    # ── fetch invoice + customer for display ──────────────────────────────────
    _step(1, 4, "Fetching invoice...")
    invoice = qbo.get_invoice(invoice_id)
    customer_ref = invoice.get("CustomerRef") or {}
    total_amt = float(invoice.get("TotalAmt", 0))
    balance   = float(invoice.get("Balance", 0))
    _done()
    _line(f"  Invoice   #{invoice.get('DocNumber')}  |  ID: {invoice_id}")
    _line(f"  Customer  {customer_ref.get('name', '—')}")
    _line(f"  Amount    {total_amt:.3f} KWD  (balance: {balance:.3f} KWD)")

    customer_id = str(customer_ref.get("value", ""))
    _step(2, 4, "Fetching customer...")
    customer = qbo.get_customer_by_id(customer_id)
    _done(f"{customer.display_name}  |  {customer.mobile or customer.phone or 'no phone'}")

    # ── dry-run mode ──────────────────────────────────────────────────────────
    if dry_run:
        _dry_run_display(invoice, customer, settings)
        return

    # ── live workflow ─────────────────────────────────────────────────────────
    _step(3, 4, "Running workflow...")
    print()

    tap = tap_client_from_settings(settings)
    from messaging.whatsapp import whatsapp_client_from_settings
    whatsapp = whatsapp_client_from_settings(settings)
    if whatsapp is None:
        print("\n  ⚠  WhatsApp not configured — set TWILIO_* vars in .env to enable\n")
    result = process_invoice(
        invoice_id,
        qbo_client=qbo,
        tap_client=tap,
        whatsapp_client=whatsapp,
        settings=settings,
    )
    _done()

    # ── result display ────────────────────────────────────────────────────────
    print()
    _hr("═")

    if isinstance(result, SkipResult):
        _fail("Invoice skipped", result.reason)
        _hr("═")
        print()
        return

    if isinstance(result, LinkResult):
        status_label = "New link generated" if result.status == "LINK_GENERATED" else "Existing link returned"
        _ok("Status",           status_label)
        _ok("Invoice ID",       result.invoice_id)
        _ok("Invoice Number",   f"#{result.invoice_number}")
        _ok("Customer",         result.customer_name)
        _ok("Amount",           f"{result.amount:.3f} {result.currency}")
        _ok("Tap Charge ID",    result.tap_charge_id)
        _ok("QBO note updated", "YES" if result.qbo_note_updated else "NO (check logs)")
        if result.whatsapp_sent:
            _ok("WhatsApp sent",    result.whatsapp_number)
        elif result.whatsapp_number:
            _fail("WhatsApp failed",  result.whatsapp_number + " — check logs")
        elif whatsapp is None:
            print(f"  ⚠   {'WhatsApp skipped':<28} TWILIO_* not configured in .env")
        elif result.status == "EXISTING_LINK_RETURNED":
            print(f"  ⚠   {'WhatsApp skipped':<28} already sent or no valid mobile")
        else:
            print(f"  ⚠   {'WhatsApp skipped':<28} no valid Kuwait mobile on record")
        print()
        _line("Payment URL:")
        _line(f"  {result.payment_url}")
        print()
        _hr()
        _step(4, 4, "Idempotency check (re-run)...")
        second = process_invoice(invoice_id, qbo_client=qbo, tap_client=tap, settings=settings)
        if isinstance(second, LinkResult) and second.status == "EXISTING_LINK_RETURNED":
            _done("PASS — same link returned, no duplicate created")
        else:
            _done("UNEXPECTED — check logs")
        _hr("═")
        print()


if __name__ == "__main__":
    main()
