"""Register payment links from bootstrap JSON (same logic as server startup).

    python -m scripts.register_existing_links
    python -m scripts.register_existing_links data/bootstrap_payment_links.json
"""
from __future__ import annotations

import sys

from db.payment_links import bootstrap_links_from_file, get_by_invoice_id, init_table


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    init_table()
    imported = bootstrap_links_from_file(path)
    print(f"Registered {imported} new payment link(s).")

    if path is None:
        path = "data/bootstrap_payment_links.json"

    from pathlib import Path
    import json

    bootstrap = Path(path)
    if bootstrap.is_file():
        for rec in json.loads(bootstrap.read_text(encoding="utf-8")):
            row = get_by_invoice_id(str(rec["invoice_id"]))
            status = row["status"] if row else "missing"
            print(f"  #{rec.get('invoice_number')}  charge {rec['tap_charge_id']}  →  {status}")


if __name__ == "__main__":
    main()
