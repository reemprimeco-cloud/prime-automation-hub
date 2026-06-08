"""Test script (success criterion): list the latest 10 invoices.

    python -m scripts.list_invoices

Prints the 10 most recent invoices (by transaction date) as a table.
"""
from __future__ import annotations

from qbo.client import QuickBooksClient


def _money(value) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def main() -> None:
    client = QuickBooksClient()
    invoices = client.get_invoices(max_results=10, order_by="TxnDate DESC")

    if not invoices:
        print("No invoices found for this company.")
        return

    print(f"Latest {len(invoices)} invoice(s):\n")
    header = f"{'Doc #':<10} {'Date':<12} {'Customer':<28} {'Total':>12} {'Balance':>12}"
    print(header)
    print("-" * len(header))
    for inv in invoices:
        doc = str(inv.get("DocNumber", "—"))
        date = str(inv.get("TxnDate", "—"))
        customer = str((inv.get("CustomerRef", {}) or {}).get("name", "—"))[:27]
        total = _money(inv.get("TotalAmt"))
        balance = _money(inv.get("Balance"))
        print(f"{doc:<10} {date:<12} {customer:<28} {total:>12} {balance:>12}")


if __name__ == "__main__":
    main()
