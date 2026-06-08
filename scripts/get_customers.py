"""List customers for the connected QuickBooks company.

    python -m scripts.get_customers
"""
from __future__ import annotations

from qbo.client import QuickBooksClient


def main() -> None:
    client = QuickBooksClient()
    customers = client.get_customers(max_results=100)
    print(f"Found {len(customers)} customer(s):\n")
    header = f"{'ID':<10} {'Display Name':<30} {'Email':<28} {'Phone':<16} {'Mobile':<16}"
    print(header)
    print("-" * len(header))
    for c in customers:
        print(
            f"{c.id:<10} {c.display_name[:29]:<30} "
            f"{c.email[:27]:<28} {c.phone[:15]:<16} {c.mobile[:15]:<16}"
        )


if __name__ == "__main__":
    main()
