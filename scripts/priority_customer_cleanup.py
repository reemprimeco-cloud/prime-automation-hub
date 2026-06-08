"""Phase 1.7.1 — High Value Customer Cleanup.

Generates reports/priority_customer_cleanup.csv containing only the customers
that have BOTH phone problems (INVALID or MISSING) AND at least one open invoice,
sorted by outstanding balance descending.

    python -m scripts.priority_customer_cleanup

This is self-contained — it re-audits phones and queries invoices fresh
from QuickBooks so the output is always current.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from qbo.client import QuickBooksClient
from qbo.models import Customer
from qbo.phone import audit_customer, best_problem, diagnose_field

OUTPUT_PATH = Path("reports/priority_customer_cleanup.csv")

CSV_COLUMNS = [
    "customer_id",
    "customer_name",
    "phone",
    "mobile",
    "problem",
    "open_invoice_count",
    "outstanding_balance",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _problem_code(c: Customer) -> str:
    """Derive the specific problem code for a customer that failed validation."""
    codes = []
    for raw in [c.mobile, c.phone, c.alternate_phone]:
        if (raw or "").strip():
            code, _ = diagnose_field(raw)
            if code:
                codes.append(code)
    return best_problem(codes) if codes else "MISSING"


def _fetch_open_invoices(
    client: QuickBooksClient, customer_ids: list[str]
) -> dict[str, dict]:
    """Return {customer_id: {"count": N, "balance": X.XXX}} for open invoices."""
    if not customer_ids:
        return {}

    or_clause = " OR ".join(f"CustomerRef = '{cid}'" for cid in customer_ids)
    stmt = (
        f"SELECT * FROM Invoice WHERE ({or_clause}) "
        f"AND Balance > '0' MAXRESULTS 1000"
    )
    invoices = (
        client.query(stmt).get("QueryResponse", {}).get("Invoice") or []
    )

    totals: dict[str, dict] = {}
    for inv in invoices:
        cid = str((inv.get("CustomerRef") or {}).get("value", ""))
        bal = float(inv.get("Balance", 0))
        if cid not in totals:
            totals[cid] = {"count": 0, "balance": 0.0}
        totals[cid]["count"]   += 1
        totals[cid]["balance"] += bal

    return totals


# ── main ──────────────────────────────────────────────────────────────────────

@dataclass
class PriorityRow:
    customer_id:        str
    customer_name:      str
    phone:              str
    mobile:             str
    problem:            str
    open_invoice_count: int
    outstanding_balance: float


def main() -> None:
    client = QuickBooksClient()
    customers = client.get_all_active_customers()

    # 1. Phone audit — keep only the failures
    flagged: list[Customer] = [
        c for c in customers
        if audit_customer(c.mobile, c.phone, c.alternate_phone).status != "READY"
    ]

    if not flagged:
        print("All active customers have valid phone numbers. Nothing to do.")
        return

    # 2. Open invoices for flagged customers — single query
    flagged_ids = [c.id for c in flagged]
    inv_totals = _fetch_open_invoices(client, flagged_ids)

    # 3. Keep only customers that have at least one open invoice
    rows: list[PriorityRow] = []
    for c in flagged:
        totals = inv_totals.get(c.id)
        if not totals:
            continue                    # phone problem but no money at risk → skip
        rows.append(PriorityRow(
            customer_id=c.id,
            customer_name=c.display_name,
            phone=c.phone,
            mobile=c.mobile,
            problem=_problem_code(c),
            open_invoice_count=totals["count"],
            outstanding_balance=round(totals["balance"], 3),
        ))

    # 4. Sort by outstanding balance descending
    rows.sort(key=lambda r: r.outstanding_balance, reverse=True)

    # 5. Write CSV
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "customer_id":        r.customer_id,
                "customer_name":      r.customer_name,
                "phone":              r.phone,
                "mobile":             r.mobile,
                "problem":            r.problem,
                "open_invoice_count": r.open_invoice_count,
                "outstanding_balance": f"{r.outstanding_balance:.3f}",
            })

    # 6. Console summary
    total_balance = sum(r.outstanding_balance for r in rows)
    generated_at  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n  Priority Customer Cleanup — {generated_at}")
    print(f"  {'─' * 70}")
    print(f"  {len(flagged)} customer(s) with phone problems  |  "
          f"{len(rows)} have open invoices  |  "
          f"{len(flagged) - len(rows)} have none\n")

    if rows:
        hdr = (f"  {'ID':<8} {'Customer':<26} {'Problem':<22} "
               f"{'Inv':>4}  {'Balance':>12}")
        print(hdr)
        print("  " + "─" * (len(hdr) - 2))
        for r in rows:
            print(
                f"  {r.customer_id:<8} {r.customer_name[:25]:<26} "
                f"{r.problem:<22} {r.open_invoice_count:>4}  "
                f"{r.outstanding_balance:>12,.3f}"
            )
        print(f"\n  {'Total outstanding':>62}  {total_balance:>12,.3f}")

    print(f"\n  Saved → {OUTPUT_PATH}\n")


if __name__ == "__main__":
    main()
