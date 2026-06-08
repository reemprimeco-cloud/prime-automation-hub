"""Discover and report all invoice custom field definitions.

    python -m scripts.inspect_custom_fields

Uses three discovery strategies (Preferences API, enhanced invoice query,
plain invoice scan) and deduplicates across them. Outputs:

    reports/custom_fields.json
    reports/custom_fields.csv

NOTE on availability
--------------------
* Standard/Plus/Essentials plans: fields defined in Settings → Custom Fields
  appear in the Preferences API and in the CustomField array of invoices.
* QuickBooks Advanced: Preferences may be unreliable due to a known API issue.
  The enhanced strategy (include=enhancedAllCustomFields, minorversion 75)
  is more reliable on Advanced plans.
* The new Premium Custom Fields GraphQL API (legacyIdV2) requires Intuit Gold
  or Platinum partner status and is outside the scope of this script.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from qbo.client import QuickBooksClient


def main() -> None:
    output_dir = Path("reports")
    output_dir.mkdir(exist_ok=True)

    client = QuickBooksClient()
    fields = client.get_invoice_custom_fields()

    generated_at = datetime.now(timezone.utc).isoformat()

    report = {
        "generated_at": generated_at,
        "realm_id": client.tokens.realm_id,
        "total_fields_found": len(fields),
        "custom_fields": fields,
    }

    json_path = output_dir / "custom_fields.json"
    json_path.write_text(json.dumps(report, indent=2))

    csv_path = output_dir / "custom_fields.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["field_id", "field_name", "data_type", "active", "source"],
        )
        writer.writeheader()
        writer.writerows(fields)

    if fields:
        col = f"{'Field ID':<12} {'Field Name':<28} {'Data Type':<20} {'Active':<8} Source"
        print(f"\nInvoice Custom Fields — {generated_at}\n")
        print(col)
        print("-" * len(col))
        for f in fields:
            print(
                f"{f['field_id']:<12} {f['field_name'][:27]:<28} "
                f"{f['data_type']:<20} {str(f['active']):<8} {f['source']}"
            )
    else:
        print(
            "\nNo invoice custom fields were found.\n"
            "This may mean:\n"
            "  • No custom fields have been defined for invoices in this company.\n"
            "  • The Preferences API is not returning them (known issue on QBO Advanced).\n"
            "  • No invoices exist yet that carry custom field data.\n"
            "  • The Premium Custom Fields API (Gold/Platinum partners only) is needed.\n"
        )

    print(f"\nSaved: {json_path}  |  {csv_path}")


if __name__ == "__main__":
    main()
