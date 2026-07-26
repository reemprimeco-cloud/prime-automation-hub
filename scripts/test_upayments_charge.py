"""Create one UPayments sandbox charge and print the hosted payment URL.

Does NOT call production. Does NOT wire into webhook routes.

Usage
-----
    # Ensure .env (or shell env) has:
    #   UPAYMENTS_API_KEY
    #   UPAYMENTS_MERCHANT_ID=78508
    #   UPAYMENTS_BASE_URL=https://sandboxapi.upayments.com/api/v1
    #   UPAYMENTS_RETURN_URL=https://prime-automation-hub.onrender.com/payment/success
    #   UPAYMENTS_CANCEL_URL=https://prime-automation-hub.onrender.com/payment/success
    #   UPAYMENTS_NOTIFICATION_URL=https://webhook.site/<your-id>   # or any HTTPS receiver

    python -m scripts.test_upayments_charge
    python -m scripts.test_upayments_charge --amount 1.000 --invoice TEST-001
    python -m scripts.test_upayments_charge --dry-run

This script is for manual sandbox verification only. Do not run against live
keys until the webhook route is deliberately switched over.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow `python -m scripts.test_upayments_charge` from repo root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from upayments.client import upayments_client_from_env
from upayments.exceptions import UPaymentsAPIError


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create a UPayments sandbox charge")
    p.add_argument("--amount", type=float, default=1.0, help="Charge amount (default 1.000)")
    p.add_argument("--currency", default="KWD")
    p.add_argument("--invoice", default="TEST-UPAY-001", help="Invoice / order reference")
    p.add_argument("--name", default="Prime Test Customer")
    p.add_argument("--phone", default="+96550000000", help="E.164 phone with country code")
    p.add_argument(
        "--description",
        default="",
        help="Order description (defaults to Invoice #<invoice>)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print request config only — do not call the API",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    description = args.description or f"Invoice #{args.invoice} — sandbox test"

    required = (
        "UPAYMENTS_API_KEY",
        "UPAYMENTS_MERCHANT_ID",
        "UPAYMENTS_RETURN_URL",
        "UPAYMENTS_CANCEL_URL",
        "UPAYMENTS_NOTIFICATION_URL",
    )
    print("UPayments sandbox charge test")
    print(f"  BASE_URL     {os.getenv('UPAYMENTS_BASE_URL', 'https://sandboxapi.upayments.com/api/v1')}")
    print(f"  MERCHANT_ID  {os.getenv('UPAYMENTS_MERCHANT_ID', '(missing)')}")
    print(f"  API_KEY      {'set' if os.getenv('UPAYMENTS_API_KEY', '').strip() else 'MISSING'}")
    print(f"  amount       {args.amount} {args.currency}")
    print(f"  invoice      {args.invoice}")
    print(f"  customer     {args.name} / {args.phone}")
    print()

    if args.dry_run:
        print("DRY RUN — no API call made.")
        print(
            json.dumps(
                {
                    "endpoint": "POST {UPAYMENTS_BASE_URL}/charge",
                    "order.id": args.invoice,
                    "order.amount": args.amount,
                    "order.currency": args.currency,
                    "customer.name": args.name,
                    "customer.mobile": args.phone,
                    "returnUrl": os.getenv("UPAYMENTS_RETURN_URL") or "(set UPAYMENTS_RETURN_URL)",
                    "cancelUrl": os.getenv("UPAYMENTS_CANCEL_URL") or "(set UPAYMENTS_CANCEL_URL)",
                    "notificationUrl": os.getenv("UPAYMENTS_NOTIFICATION_URL")
                    or "(set UPAYMENTS_NOTIFICATION_URL)",
                },
                indent=2,
            )
        )
        return

    missing = [k for k in required if not os.getenv(k, "").strip()]
    if missing:
        print("Missing env vars:", ", ".join(missing), file=sys.stderr)
        print("Copy from .env.example and fill in sandbox values.", file=sys.stderr)
        raise SystemExit(1)

    try:
        client = upayments_client_from_env()
        result = client.create_charge(
            amount=args.amount,
            currency=args.currency,
            customer_name=args.name,
            customer_phone=args.phone,
            invoice_number=args.invoice,
            description=description,
        )
    except UPaymentsAPIError as exc:
        print(f"UPaymentsAPIError: {exc}", file=sys.stderr)
        if exc.status_code is not None:
            print(f"  status_code: {exc.status_code}", file=sys.stderr)
        if exc.body:
            print(f"  body: {exc.body[:500]}", file=sys.stderr)
        raise SystemExit(1)

    print("Charge created:")
    print(f"  charge_id    {result.get('charge_id')}")
    print(f"  payment_url  {result.get('payment_url')}")
    print(f"  status       {result.get('status')}")
    print()
    print("Full response body:")
    print(json.dumps(result.get("raw") or result, indent=2, default=str))
    print()
    print("Open payment_url in a browser and confirm the charge in the sandbox dashboard.")


if __name__ == "__main__":
    main()
