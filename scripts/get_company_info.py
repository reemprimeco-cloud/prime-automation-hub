"""Print Company Info for the connected QuickBooks company.

    python -m scripts.get_company_info
"""
from __future__ import annotations

import json

from qbo.client import QuickBooksClient


def main() -> None:
    client = QuickBooksClient()
    info = client.get_company_info()
    name = info.get("CompanyName", "(unknown)")
    country = info.get("Country", "")
    print(f"Company: {name}  {('(' + country + ')') if country else ''}\n")
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
