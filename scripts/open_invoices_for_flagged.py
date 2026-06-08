"""Find all open invoices belonging to customers that failed the phone audit.

Reads customer IDs from the cleanup exports produced by customer_data_cleanup.py,
then queries QuickBooks for every invoice with an outstanding balance.

    python -m scripts.open_invoices_for_flagged

Prerequisites
-------------
Run scripts.customer_data_cleanup first so these files exist:
    reports/invalid_customers.csv
    reports/missing_customers.csv
"""
from __future__ import annotations

import csv
from pathlib import Path

from qbo.client import QuickBooksClient

INVALID_CSV = Path("reports/invalid_customers.csv")
MISSING_CSV = Path("reports/missing_customers.csv")


def _load_customer_ids(*paths: Path) -> dict[str, str]:
    """Return {customer_id: customer_name} from one or more cleanup CSVs."""
    customers: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                cid = (row.get("customer_id") or "").strip()
                name = (row.get("customer_name") or "").strip()
                if cid:
                    customers[cid] = name
    return customers


def _fetch_open_invoices(client: QuickBooksClient, customer_ids: list[str]) -> list[dict]:
    """Return all open invoices (Balance > 0) for the given customer IDs."""
    if not customer_ids:
        return []

    # Build OR chain — fine for small sets; QBO supports it
    or_clause = " OR ".join(f"CustomerRef = '{cid}'" for cid in customer_ids)
    stmt = f"SELECT * FROM Invoice WHERE ({or_clause}) AND Balance > '0' MAXRESULTS 1000"

    data = client.query(stmt)
    return (data.get("QueryResponse") or {}).get("Invoice") or []


def main() -> None:
    customers = _load_customer_ids(INVALID_CSV, MISSING_CSV)

    if not customers:
        print(
            "No flagged customers found.\n"
            "Run `python -m scripts.customer_data_cleanup` first to generate the CSVs."
        )
        return

    print(f"\nQuerying open invoices for {len(customers)} flagged customer(s)…\n")

    client = QuickBooksClient()
    invoices = _fetch_open_invoices(client, list(customers.keys()))

    total_outstanding = sum(float(inv.get("Balance", 0)) for inv in invoices)
    customer_ids_with_invoices = {
        str((inv.get("CustomerRef") or {}).get("value", "")) for inv in invoices
    }

    # ── summary ───────────────────────────────────────────────────────────────
    print(f"  Open invoices found : {len(invoices)}")
    print(f"  Total outstanding   : {total_outstanding:,.3f} KWD")
    print(f"  Customers affected  : {len(customer_ids_with_invoices)} "
          f"of {len(customers)} flagged\n")

    # ── per-invoice table ─────────────────────────────────────────────────────
    if invoices:
        hdr = f"  {'Invoice #':<12} {'Date':<12} {'Customer':<28} {'Total':>10} {'Balance':>10}"
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))
        for inv in sorted(invoices, key=lambda i: i.get("TxnDate", ""), reverse=True):
            doc       = str(inv.get("DocNumber", "—"))
            date      = str(inv.get("TxnDate", "—"))
            cust_name = str((inv.get("CustomerRef") or {}).get("name", "—"))[:27]
            total     = float(inv.get("TotalAmt", 0))
            balance   = float(inv.get("Balance", 0))
            print(f"  {doc:<12} {date:<12} {cust_name:<28} {total:>10,.3f} {balance:>10,.3f}")

    # ── customers with no open invoices ───────────────────────────────────────
    clean = {cid: name for cid, name in customers.items()
             if cid not in customer_ids_with_invoices}
    if clean:
        print(f"\n  Flagged customers with no open invoices ({len(clean)}):")
        for cid, name in clean.items():
            print(f"    [{cid}] {name}")


if __name__ == "__main__":
    main()
