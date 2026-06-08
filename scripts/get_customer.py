"""Retrieve a single customer by their QuickBooks ID.

    python -m scripts.get_customer <customer_id>

Example:
    python -m scripts.get_customer 42
"""
from __future__ import annotations

import sys

from qbo.client import QBOError, QuickBooksClient


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.get_customer <customer_id>")
        print("  customer_id — the numeric QuickBooks ID (e.g. 42)")
        sys.exit(1)

    customer_id = sys.argv[1]
    client = QuickBooksClient()

    try:
        c = client.get_customer_by_id(customer_id)
    except QBOError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Customer ID:    {c.id}")
    print(f"Display Name:   {c.display_name or '—'}")
    print(f"Email:          {c.email  or '—'}")
    print(f"Phone:          {c.phone  or '—'}")
    print(f"Mobile:         {c.mobile or '—'}")


if __name__ == "__main__":
    main()
