#!/usr/bin/env python3
"""Self-contained UPayments live/sandbox charge test (no package install needed).

Save as:  scripts/test_upayments_live.py
Run:
    cd ~/Documents/prime-automation-hub
    source .venv/bin/activate
    python scripts/test_upayments_live.py --dry-run
    python scripts/test_upayments_live.py

Requires in .env:
    UPAYMENTS_API_KEY=...
    UPAYMENTS_MERCHANT_ID=78508
    UPAYMENTS_BASE_URL=https://uapi.upayments.com/api/v1
    UPAYMENTS_RETURN_URL=https://prime-automation-hub.onrender.com/payment/success
    UPAYMENTS_CANCEL_URL=https://prime-automation-hub.onrender.com/payment/success
    UPAYMENTS_NOTIFICATION_URL=https://webhook.site/your-id
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

import requests

ROOT = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(ROOT / ".env")


class UPaymentsAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return f"+{digits}" if digits else ""


def create_charge(
    *,
    api_key: str,
    merchant_id: str,
    base_url: str,
    amount: float,
    currency: str,
    customer_name: str,
    customer_phone: str,
    invoice_number: str,
    description: str,
    return_url: str,
    cancel_url: str,
    notification_url: str,
) -> dict:
    base = base_url.rstrip("/")
    order_id = invoice_number.strip().lstrip("#")
    name = (customer_name or "").strip() or "Customer"
    body = {
        "order": {
            "id": order_id[:40],
            "reference": order_id[:255],
            "description": (description or f"Invoice #{order_id}")[:500],
            "currency": currency.upper(),
            "amount": round(float(amount), 3),
        },
        "language": "en",
        "reference": {"id": order_id[:35]},
        "customer": {
            "uniqueId": order_id[:50],
            "name": name[:50],
            "email": f"invoice-{order_id}@prime.local",
        },
        "returnUrl": return_url,
        "cancelUrl": cancel_url,
        "notificationUrl": notification_url,
        "customerExtraData": f"merchant_id={merchant_id};invoice={order_id}",
    }
    mobile = _phone(customer_phone)
    if mobile:
        body["customer"]["mobile"] = mobile

    resp = requests.post(
        f"{base}/charge",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Merchant-Id": str(merchant_id),
        },
        json=body,
        timeout=30,
    )
    text = resp.text[:2000]
    if not resp.ok:
        raise UPaymentsAPIError(
            f"UPayments HTTP {resp.status_code}: {text}",
            status_code=resp.status_code,
            body=text,
        )
    data = resp.json()
    if isinstance(data, dict) and data.get("status") is False:
        raise UPaymentsAPIError(
            f"UPayments API error: {data.get('message') or text}",
            status_code=resp.status_code,
            body=text,
        )
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    payment_url = str(
        nested.get("link")
        or nested.get("paymentURL")
        or nested.get("payment_url")
        or nested.get("paymentUrl")
        or nested.get("redirect_url")
        or nested.get("url")
        or ""
    ).strip()
    charge_id = str(
        nested.get("track_id")
        or nested.get("payment_id")
        or nested.get("invoice_id")
        or nested.get("order_id")
        or nested.get("id")
        or ""
    ).strip()
    if not payment_url:
        raise UPaymentsAPIError(f"Missing payment URL in response: {text}")
    return {
        "payment_url": payment_url,
        "charge_id": charge_id,
        "raw": data,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--amount", type=float, default=1.0)
    p.add_argument("--currency", default="KWD")
    p.add_argument("--invoice", default="LIVE-TEST-001")
    p.add_argument("--name", default="Prime Test Customer")
    p.add_argument("--phone", default="+96550000000")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    api_key = os.getenv("UPAYMENTS_API_KEY", "").strip()
    merchant_id = os.getenv("UPAYMENTS_MERCHANT_ID", "78508").strip()
    base_url = os.getenv(
        "UPAYMENTS_BASE_URL", "https://uapi.upayments.com/api/v1"
    ).strip()
    return_url = os.getenv("UPAYMENTS_RETURN_URL", "").strip()
    cancel_url = os.getenv("UPAYMENTS_CANCEL_URL", "").strip()
    notification_url = os.getenv("UPAYMENTS_NOTIFICATION_URL", "").strip()

    print("UPayments charge test")
    print(f"  BASE_URL     {base_url}")
    print(f"  MERCHANT_ID  {merchant_id}")
    print(f"  API_KEY      {'set' if api_key else 'MISSING'}")
    print(f"  amount       {args.amount} {args.currency}")
    print(f"  invoice      {args.invoice}")
    print()

    if args.dry_run:
        print("DRY RUN — no API call.")
        print(json.dumps({
            "endpoint": f"POST {base_url}/charge",
            "merchant_id": merchant_id,
            "order.id": args.invoice,
            "amount": args.amount,
            "returnUrl": return_url or "(missing)",
            "cancelUrl": cancel_url or "(missing)",
            "notificationUrl": notification_url or "(missing)",
        }, indent=2))
        return

    missing = [n for n, v in [
        ("UPAYMENTS_API_KEY", api_key),
        ("UPAYMENTS_RETURN_URL", return_url),
        ("UPAYMENTS_CANCEL_URL", cancel_url),
        ("UPAYMENTS_NOTIFICATION_URL", notification_url),
    ] if not v]
    if missing:
        print("Missing:", ", ".join(missing), file=sys.stderr)
        raise SystemExit(1)

    try:
        result = create_charge(
            api_key=api_key,
            merchant_id=merchant_id,
            base_url=base_url,
            amount=args.amount,
            currency=args.currency,
            customer_name=args.name,
            customer_phone=args.phone,
            invoice_number=args.invoice,
            description=f"Invoice #{args.invoice} — live test",
            return_url=return_url,
            cancel_url=cancel_url,
            notification_url=notification_url,
        )
    except UPaymentsAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

    print("Charge created:")
    print(f"  charge_id    {result['charge_id']}")
    print(f"  payment_url  {result['payment_url']}")
    print()
    print(json.dumps(result["raw"], indent=2, default=str))


if __name__ == "__main__":
    main()
