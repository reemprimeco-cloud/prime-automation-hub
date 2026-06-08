"""Check whether Render automation is ready (no manual capture needed).

    python -m scripts.check_automation

Probes the live hub and reports what is working vs what still needs attention.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HUB_URL = os.getenv("HUB_URL", "https://prime-automation-hub.onrender.com").rstrip("/")


def _get(path: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(f"{HUB_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _post(path: str, payload: dict) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{HUB_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


def _ok(label: str) -> None:
    print(f"  ✓  {label}")


def _warn(label: str) -> None:
    print(f"  ⚠  {label}")


def _fail(label: str) -> None:
    print(f"  ✗  {label}")


def main() -> None:
    print(f"\nAutomation check — {HUB_URL}\n")

    status, health = _get("/health")
    if status != 200 or not isinstance(health, dict):
        _fail(f"/health returned {status}")
        sys.exit(1)

    _ok("Hub is healthy")
    if health.get("qbo_token_configured"):
        _ok("QBO webhook verifier configured on Render")
    else:
        _fail("QBO_WEBHOOK_VERIFIER_TOKEN missing on Render")

    events = health.get("recent_events") or []
    if events:
        _ok(f"QBO webhooks reaching Render ({len(events)} recent event(s))")
        creates = [e for e in events if e.get("operation") == "Create"]
        if creates:
            _ok("Invoice Create events seen — auto link generation can run")
        else:
            _warn("Only Update/Delete events seen so far — Create triggers auto links")
    else:
        _warn("No QBO webhook events stored yet — confirm Intuit URL points to Render /webhook")

    status, success = _get("/payment/success")
    if status == 200 and isinstance(success, str) and "Payment successful" in success:
        _ok("Tap redirect success page is live")
    else:
        _fail(f"/payment/success returned {status}")

    probe_charge = "chg_automation_probe_not_real"
    status, tap = _post(
        "/webhook/tap",
        {"id": probe_charge, "status": "CAPTURED", "amount": 1, "currency": "KWD"},
    )
    if status == 200 and isinstance(tap, dict) and tap.get("status") == "unknown_charge":
        _ok("Tap webhook handler works (DB ready, unknown charges handled safely)")
    elif status == 500 and isinstance(tap, dict) and "payment_links" in str(tap.get("detail", "")):
        _fail("payment_links table missing on Render — redeploy latest code")
        sys.exit(1)
    else:
        _fail(f"Unexpected Tap webhook response ({status}): {tap}")
        sys.exit(1)

    print()
    print("Fully automated flow (no manual scripts):")
    print("  1. Create invoice in QBO → Render /webhook → Tap link + WhatsApp")
    print("  2. Customer pays → Render /webhook/tap → QBO Paid + WhatsApp confirmation")
    print()
    print("Note: links created locally (test_invoice_to_tap on your Mac) are NOT on Render.")
    print("      Only invoices processed on Render can auto-capture after payment.")
    print()


if __name__ == "__main__":
    main()
