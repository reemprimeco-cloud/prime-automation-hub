"""Generate the final verification report.

    python -m scripts.generate_report

Outputs:
    reports/company_report.json   — full report (company, customers, invoices, webhook)
    reports/company_report.csv    — flat invoice table

Report contents
---------------
1. Company Name
2. Total Customers
3. Latest 10 Invoices with Invoice IDs, Customer IDs, Balances
4. Invoice Balances
5. Webhook Status (configured, events stored)
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from qbo.client import QuickBooksClient
from webhook.storage import count_all_events


def _build_invoice_row(inv: dict) -> dict:
    customer_ref = inv.get("CustomerRef") or {}
    return {
        "invoice_id":    str(inv.get("Id", "")),
        "doc_number":    str(inv.get("DocNumber", "")),
        "date":          str(inv.get("TxnDate", "")),
        "due_date":      str(inv.get("DueDate", "")),
        "customer_id":   str(customer_ref.get("value", "")),
        "customer_name": str(customer_ref.get("name", "")),
        "total":         float(inv.get("TotalAmt", 0)),
        "balance":       float(inv.get("Balance", 0)),
        "status":        "Paid" if float(inv.get("Balance", 1)) == 0 else "Outstanding",
    }


def _webhook_status() -> dict:
    token_set = bool(os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip())
    db_path = os.getenv("WEBHOOK_DB_PATH", "webhook_events.db")
    db_exists = os.path.exists(db_path)
    events = count_all_events()

    status = "not_configured"
    if token_set and db_exists:
        status = "active" if events > 0 else "configured_no_events"
    elif token_set:
        status = "configured_pending_start"

    return {
        "status": status,
        "verifier_token_set": token_set,
        "events_db_path": db_path,
        "db_exists": db_exists,
        "events_stored": events,
    }


def main() -> None:
    output_dir = Path("reports")
    output_dir.mkdir(exist_ok=True)

    client = QuickBooksClient()

    # 1. Company name
    info = client.get_company_info()
    company_name = info.get("CompanyName", "(unknown)")

    # 2. Total customers
    count_data = client.query("SELECT COUNT(*) FROM Customer")
    total_customers = int(
        (count_data.get("QueryResponse") or {}).get("totalCount", 0)
    )

    # 3–6. Latest 10 invoices
    raw_invoices = client.get_invoices(max_results=10)
    invoices = [_build_invoice_row(inv) for inv in raw_invoices]

    # 7. Webhook status
    webhook = _webhook_status()

    generated_at = datetime.now(timezone.utc).isoformat()
    report = {
        "generated_at":   generated_at,
        "company_name":   company_name,
        "realm_id":       client.tokens.realm_id,
        "total_customers": total_customers,
        "latest_invoices": invoices,
        "webhook_status": webhook,
    }

    # ── JSON ──────────────────────────────────────────────────────────────
    json_path = output_dir / "company_report.json"
    json_path.write_text(json.dumps(report, indent=2))

    # ── CSV ───────────────────────────────────────────────────────────────
    csv_path = output_dir / "company_report.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)

        # Summary header block
        writer.writerow(["Company Report"])
        writer.writerow(["Generated At", generated_at])
        writer.writerow(["Company Name",  company_name])
        writer.writerow(["Realm ID",      report["realm_id"]])
        writer.writerow(["Total Customers", total_customers])
        writer.writerow([])

        # Webhook block
        writer.writerow(["Webhook Status"])
        for k, v in webhook.items():
            writer.writerow([k, v])
        writer.writerow([])

        # Invoice table
        writer.writerow(["Latest Invoices"])
        if invoices:
            inv_writer = csv.DictWriter(
                fh,
                fieldnames=list(invoices[0].keys()),
            )
            inv_writer.writeheader()
            inv_writer.writerows(invoices)
        else:
            writer.writerow(["No invoices found."])

    # ── Console summary ───────────────────────────────────────────────────
    print(f"\n{'═' * 55}")
    print(f"  Company Report — {generated_at}")
    print(f"{'═' * 55}")
    print(f"  Company:          {company_name}")
    print(f"  Realm ID:         {report['realm_id']}")
    print(f"  Total Customers:  {total_customers}")
    print(f"  Webhook:          {webhook['status']} ({webhook['events_stored']} events)")
    print()

    if invoices:
        hdr = f"  {'ID':<8} {'Doc#':<8} {'Date':<12} {'Customer ID':<14} {'Total':>10} {'Balance':>10}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for inv in invoices:
            print(
                f"  {inv['invoice_id']:<8} {inv['doc_number']:<8} "
                f"{inv['date']:<12} {inv['customer_id']:<14} "
                f"{inv['total']:>10.2f} {inv['balance']:>10.2f}"
            )

    print(f"\n  Saved → {json_path}  |  {csv_path}\n")


if __name__ == "__main__":
    main()
