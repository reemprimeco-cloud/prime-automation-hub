"""Phase 1.6 — Customer Data Cleanup.

Fetches every active customer, re-audits their phone fields with detailed
diagnosis, and produces two targeted export files for manual correction.

    python -m scripts.customer_data_cleanup

Output
------
    reports/invalid_customers.csv    — customers with phone data that fails
    reports/missing_customers.csv    — customers with no phone data at all
    reports/cleanup_summary.json     — machine-readable summary

Columns (both CSVs)
-------------------
    customer_id, customer_name,
    primary_phone, mobile, alternate_phone,
    problem, suggested

Problem codes
-------------
    MISSING            All phone fields empty — needs data entry
    LANDLINE           Kuwait number but not a mobile prefix (2xx) — ask for mobile
    INVALID_LENGTH     Wrong digit count — likely a typo
    INVALID_COUNTRY    Foreign country code — entered wrong number
    UNSUPPORTED_FORMAT No recognisable digits — garbage or placeholder text

Success criterion
-----------------
Script exits 0 only when the combined INVALID + MISSING list is complete
(every non-READY active customer is present in one of the two files).
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from logging_config import get_logger
from qbo.client import QuickBooksClient
from qbo.models import Customer
from qbo.phone import audit_customer, best_problem, diagnose_field

_LOG = get_logger("cleanup")

REPORT_DIR = Path("reports")
INVALID_CSV = REPORT_DIR / "invalid_customers.csv"
MISSING_CSV = REPORT_DIR / "missing_customers.csv"
SUMMARY_JSON = REPORT_DIR / "cleanup_summary.json"

_COLUMNS = [
    "customer_id",
    "customer_name",
    "primary_phone",
    "mobile",
    "alternate_phone",
    "problem",
    "suggested",
]


# ── per-customer diagnosis ────────────────────────────────────────────────────

@dataclass
class CleanupRow:
    customer_id:    str
    customer_name:  str
    primary_phone:  str
    mobile:         str
    alternate_phone: str
    problem:        str
    suggested:      str


def _diagnose_customer(c: Customer) -> CleanupRow:
    """
    Build a CleanupRow for a customer already known to need manual correction.

    Algorithm
    ---------
    1. Diagnose each of the three phone fields individually.
    2. Collect problem codes and suggestions from non-empty fields.
    3. The row's Problem = highest-priority code (via best_problem()).
    4. The row's Suggested = first usable E.164 suggestion found, or "".
    """
    fields = [
        ("primary_phone",   c.phone),
        ("mobile",          c.mobile),
        ("alternate_phone", c.alternate_phone),
    ]

    all_codes:  list[str] = []
    suggestions: list[str] = []

    for _, raw in fields:
        if not (raw or "").strip():
            continue                          # skip truly empty fields
        code, suggestion = diagnose_field(raw)
        if code:                              # None means "actually valid" — skip
            all_codes.append(code)
        if suggestion:
            suggestions.append(suggestion)

    # Customer with no populated fields at all — should only happen for MISSING
    if not all_codes:
        all_codes = ["MISSING"]

    return CleanupRow(
        customer_id=c.id,
        customer_name=c.display_name,
        primary_phone=c.phone,
        mobile=c.mobile,
        alternate_phone=c.alternate_phone,
        problem=best_problem(all_codes),
        suggested=suggestions[0] if suggestions else "",
    )


# ── report writing ────────────────────────────────────────────────────────────

def _write_csv(path: Path, rows: list[CleanupRow]) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "customer_id":    r.customer_id,
                "customer_name":  r.customer_name,
                "primary_phone":  r.primary_phone,
                "mobile":         r.mobile,
                "alternate_phone": r.alternate_phone,
                "problem":        r.problem,
                "suggested":      r.suggested,
            })


# ── console output ────────────────────────────────────────────────────────────

def _print_banner(total: int, ready: int, invalid: int, missing: int) -> None:
    needs = invalid + missing
    ready_pct   = ready   / total * 100
    invalid_pct = invalid / total * 100
    missing_pct = missing / total * 100
    needs_pct   = needs   / total * 100

    print(f"\n{'═' * 58}")
    print(f"  Phase 1.6 — Customer Data Cleanup")
    print(f"{'═' * 58}")
    print(f"  Active customers : {total}")
    print(f"  READY (✓ valid)  : {ready:>5}  ({ready_pct:.1f}%)")
    print(f"  INVALID          : {invalid:>5}  ({invalid_pct:.1f}%)")
    print(f"  MISSING          : {missing:>5}  ({missing_pct:.1f}%)")
    print(f"  Needs correction : {needs:>5}  ({needs_pct:.1f}%)")


def _print_problem_breakdown(invalid_rows: list[CleanupRow]) -> None:
    if not invalid_rows:
        return
    from collections import Counter
    counts = Counter(r.problem for r in invalid_rows)
    print(f"\n── INVALID breakdown ──────────────────────────────")
    for problem, count in sorted(counts.items(), key=lambda x: -x[1]):
        pct = count / len(invalid_rows) * 100
        print(f"  {problem:<22}  {count:>5}  ({pct:.1f}% of INVALID)")


def _print_sample(rows: list[CleanupRow], label: str, n: int = 5) -> None:
    subset = rows[:n]
    if not subset:
        return
    shown = min(n, len(subset))
    print(f"\n── {label} sample (first {shown}) ──────────────────────────")
    print(f"  {'ID':<8} {'Name':<24} {'Problem':<22} Suggested")
    print(f"  {'─'*7}  {'─'*23}  {'─'*21}  {'─'*20}")
    for r in subset:
        print(f"  {r.customer_id:<8} {r.customer_name[:23]:<24} "
              f"{r.problem:<22} {r.suggested or '—'}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    generated_at = datetime.now(timezone.utc).isoformat()

    client = QuickBooksClient()
    customers = client.get_all_active_customers()
    total = len(customers)

    if total == 0:
        print("No active customers found.")
        return

    # ── triage every customer ─────────────────────────────────────────────────
    ready_count = 0
    invalid_rows: list[CleanupRow] = []
    missing_rows: list[CleanupRow] = []

    for c in customers:
        audit = audit_customer(
            mobile=c.mobile,
            phone=c.phone,
            alternate_phone=c.alternate_phone,
        )

        if audit.status == "READY":
            ready_count += 1
            continue

        row = _diagnose_customer(c)

        if audit.status == "MISSING":
            row.problem = "MISSING"   # override — always explicit for missing
            missing_rows.append(row)
        else:
            invalid_rows.append(row)

        _LOG.debug(
            "customer_needs_cleanup",
            extra={"id": c.id, "status": audit.status, "problem": row.problem},
        )

    # ── console report ────────────────────────────────────────────────────────
    _print_banner(total, ready_count, len(invalid_rows), len(missing_rows))
    _print_problem_breakdown(invalid_rows)
    _print_sample(invalid_rows, "INVALID")
    _print_sample(missing_rows, "MISSING")

    # ── write files ───────────────────────────────────────────────────────────
    _write_csv(INVALID_CSV, invalid_rows)
    _write_csv(MISSING_CSV, missing_rows)

    from collections import Counter
    invalid_problem_counts = Counter(r.problem for r in invalid_rows)

    summary = {
        "generated_at":    generated_at,
        "realm_id":        client.tokens.realm_id,
        "total_active":    total,
        "ready":           {"count": ready_count,
                            "pct": round(ready_count / total * 100, 1)},
        "invalid":         {
            "count": len(invalid_rows),
            "pct":   round(len(invalid_rows) / total * 100, 1),
            "by_problem": {
                p: {"count": c, "pct": round(c / total * 100, 1)}
                for p, c in invalid_problem_counts.items()
            },
        },
        "missing":         {"count": len(missing_rows),
                            "pct": round(len(missing_rows) / total * 100, 1)},
        "total_needing_correction": len(invalid_rows) + len(missing_rows),
        "files": {
            "invalid_customers": str(INVALID_CSV),
            "missing_customers": str(MISSING_CSV),
        },
    }

    REPORT_DIR.mkdir(exist_ok=True)
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))

    _LOG.info(
        "cleanup_export_complete",
        extra={
            "invalid": len(invalid_rows),
            "missing": len(missing_rows),
            "total_needing_correction": summary["total_needing_correction"],
        },
    )

    print(f"\n  Files written:")
    print(f"    {INVALID_CSV}  ({len(invalid_rows)} rows)")
    print(f"    {MISSING_CSV}  ({len(missing_rows)} rows)")
    print(f"    {SUMMARY_JSON}\n")


if __name__ == "__main__":
    main()
