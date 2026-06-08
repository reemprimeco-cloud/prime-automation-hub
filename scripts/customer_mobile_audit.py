"""Phase 1.5 — Customer Mobile Audit.

Fetches all active customers, audits every phone field for WhatsApp eligibility,
and generates a detailed CSV report.

    python -m scripts.customer_mobile_audit

Output files
------------
    reports/customer_mobile_audit.csv    — per-customer audit rows
    reports/customer_mobile_audit.json   — summary stats (machine-readable)

Success criterion
-----------------
≥ 95 % of active customers must be READY (valid Kuwait mobile found).
The script exits with code 1 if the threshold is not met.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from logging_config import get_logger
from qbo.client import QuickBooksClient
from qbo.models import Customer
from qbo.phone import MOBILE_PREFIXES, audit_customer, is_kuwait_mobile, normalize

_LOG = get_logger("audit.mobile")

READY_THRESHOLD = 0.95  # 95 %

REPORT_DIR = Path("reports")
CSV_PATH = REPORT_DIR / "customer_mobile_audit.csv"
JSON_PATH = REPORT_DIR / "customer_mobile_audit.json"

CSV_COLUMNS = [
    "customer_id",
    "customer_name",
    "source_field",
    "raw_phone",
    "normalized_phone",
    "valid_whatsapp",
    "status",
]


# ── field analysis ────────────────────────────────────────────────────────────

def _field_analysis(customers: list[Customer]) -> dict[str, int]:
    """Count how many customers have each phone field non-empty."""
    return {
        "Mobile":         sum(1 for c in customers if c.mobile),
        "PrimaryPhone":   sum(1 for c in customers if c.phone),
        "AlternatePhone": sum(1 for c in customers if c.alternate_phone),
    }


def _valid_mobile_by_field(customers: list[Customer]) -> dict[str, int]:
    """Count how many customers have a valid Kuwait mobile in each specific field."""
    counts: dict[str, int] = {"Mobile": 0, "PrimaryPhone": 0, "AlternatePhone": 0}
    for c in customers:
        for field, raw in [
            ("Mobile", c.mobile),
            ("PrimaryPhone", c.phone),
            ("AlternatePhone", c.alternate_phone),
        ]:
            norm = normalize(raw)
            if is_kuwait_mobile(norm):
                counts[field] += 1
    return counts


def _best_field(valid_counts: dict[str, int]) -> str:
    return max(valid_counts, key=lambda k: valid_counts[k])


# ── report writing ────────────────────────────────────────────────────────────

def _write_csv(rows: list[dict]) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(summary: dict) -> None:
    REPORT_DIR.mkdir(exist_ok=True)
    JSON_PATH.write_text(json.dumps(summary, indent=2))


# ── console output ────────────────────────────────────────────────────────────

def _print_field_analysis(
    field_pop: dict[str, int],
    valid_counts: dict[str, int],
    total: int,
    best: str,
) -> None:
    print("\n── Field Popularity ──────────────────────────────────")
    print(f"  {'Field':<16} {'Non-empty':>10}   {'Valid KW Mobile':>16}")
    print(f"  {'─' * 14}   {'─' * 10}   {'─' * 16}")
    for field in ("Mobile", "PrimaryPhone", "AlternatePhone"):
        pop = field_pop[field]
        val = valid_counts[field]
        tag = " ◄ BEST" if field == best else ""
        print(
            f"  {field:<16} {pop:>5}/{total:<5} ({pop/total*100:5.1f}%)"
            f"   {val:>5}/{total:<5} ({val/total*100:5.1f}%){tag}"
        )
    print()
    print(f"  Recommendation: Use {best} as primary source.\n")


def _print_audit_summary(
    total: int,
    ready: int,
    invalid: int,
    missing: int,
    threshold_met: bool,
) -> None:
    pct = ready / total * 100 if total else 0
    threshold_pct = READY_THRESHOLD * 100
    flag = "✓ THRESHOLD MET" if threshold_met else f"✗ BELOW {threshold_pct:.0f}% THRESHOLD"

    print("── Audit Results ─────────────────────────────────────")
    print(f"  READY    (valid Kuwait mobile): {ready:>5} / {total}  ({pct:.1f}%)")
    print(f"  INVALID  (data present, fails): {invalid:>5} / {total}  ({invalid/total*100:.1f}%)")
    print(f"  MISSING  (no phone data):       {missing:>5} / {total}  ({missing/total*100:.1f}%)")
    print(f"\n  WhatsApp Readiness: {pct:.1f}%  [{flag}]")


def _print_samples(rows: list[dict], status: str, n: int = 5) -> None:
    subset = [r for r in rows if r["status"] == status][:n]
    if not subset:
        return
    print(f"\n── {status} samples (first {min(n, len(subset))}) ──────────────────────")
    for r in subset:
        name = r["customer_name"][:25]
        raw = r["raw_phone"] or "(empty)"
        norm = r["normalized_phone"] or "—"
        print(f"  [{r['customer_id']}] {name:<26} raw={raw!r:<20}  norm={norm}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    print(f"\n{'═' * 55}")
    print(f"  Customer Mobile Audit  —  {generated_at[:10]}")
    print(f"{'═' * 55}")

    client = QuickBooksClient()
    customers = client.get_all_active_customers()
    total = len(customers)

    if total == 0:
        print("\nNo active customers found.")
        return

    print(f"\n  {total} active customer(s) found.")

    # ── analysis ──────────────────────────────────────────────────────────────
    field_pop = _field_analysis(customers)
    valid_counts = _valid_mobile_by_field(customers)
    best = _best_field(valid_counts)
    _print_field_analysis(field_pop, valid_counts, total, best)

    # ── audit every customer ──────────────────────────────────────────────────
    csv_rows: list[dict] = []
    for c in customers:
        result = audit_customer(
            mobile=c.mobile,
            phone=c.phone,
            alternate_phone=c.alternate_phone,
        )
        csv_rows.append({
            "customer_id":       c.id,
            "customer_name":     c.display_name,
            "source_field":      result.source_field,
            "raw_phone":         result.raw_phone,
            "normalized_phone":  result.normalized_phone,
            "valid_whatsapp":    result.valid_whatsapp,
            "status":            result.status,
        })
        _LOG.debug(
            "customer_audited",
            extra={
                "customer_id": c.id,
                "status": result.status,
                "source": result.source_field,
            },
        )

    ready   = sum(1 for r in csv_rows if r["status"] == "READY")
    invalid = sum(1 for r in csv_rows if r["status"] == "INVALID")
    missing = sum(1 for r in csv_rows if r["status"] == "MISSING")
    threshold_met = (ready / total) >= READY_THRESHOLD

    _print_audit_summary(total, ready, invalid, missing, threshold_met)

    if invalid:
        _print_samples(csv_rows, "INVALID")
    if missing:
        _print_samples(csv_rows, "MISSING")

    # ── write reports ─────────────────────────────────────────────────────────
    _write_csv(csv_rows)

    summary = {
        "generated_at": generated_at,
        "realm_id": client.tokens.realm_id,
        "total_active_customers": total,
        "field_population": {k: {"count": v, "pct": round(v / total * 100, 1)} for k, v in field_pop.items()},
        "valid_mobile_by_field": {k: {"count": v, "pct": round(v / total * 100, 1)} for k, v in valid_counts.items()},
        "recommended_source_field": best,
        "audit_results": {
            "READY":   {"count": ready,   "pct": round(ready / total * 100, 1)},
            "INVALID": {"count": invalid, "pct": round(invalid / total * 100, 1)},
            "MISSING": {"count": missing, "pct": round(missing / total * 100, 1)},
        },
        "threshold_pct": READY_THRESHOLD * 100,
        "threshold_met": threshold_met,
    }
    _write_json(summary)

    _LOG.info(
        "mobile_audit_complete",
        extra={
            "total": total,
            "ready": ready,
            "threshold_met": threshold_met,
        },
    )

    print(f"\n  Reports saved:")
    print(f"    {CSV_PATH}")
    print(f"    {JSON_PATH}\n")

    if not threshold_met:
        sys.exit(1)


if __name__ == "__main__":
    main()
