"""Comprehensive system check for Prime Automation Hub.

Run this any time you suspect something is wrong, or before creating
a new invoice if you want extra confidence.

Usage:
    python -m scripts.full_check
"""
from __future__ import annotations

import json
import sys

import requests

BASE_URL = "https://prime-automation-hub.onrender.com"


def _check(name: str, condition: bool, detail: str = "") -> bool:
    icon = "✅" if condition else "❌"
    line = f"{icon}  {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    return condition


def main() -> None:
    print(f"Automation check — {BASE_URL}\n")
    all_ok = True

    # ── 1. Basic reachability ───────────────────────────────────────────────
    try:
        resp = requests.get(f"{BASE_URL}/health", timeout=15)
        data = resp.json()
    except Exception as exc:
        print(f"❌  Could not reach the Hub at all: {exc}")
        sys.exit(1)

    all_ok &= _check("Hub is reachable", resp.status_code == 200)
    all_ok &= _check(
        "QBO webhook verifier configured",
        data.get("qbo_webhook_verifier_configured", False),
    )
    all_ok &= _check("QBO tokens present", data.get("qbo_token_configured", False))

    qbo_ok = data.get("qbo_api_ok", False)
    all_ok &= _check(
        "QBO API call succeeds (tokens valid)",
        qbo_ok,
        data.get("qbo_api_error") or "",
    )

    events_stored = data.get("qbo_webhook_events_stored", data.get("tap_events_stored", 0))
    last_webhook = data.get("last_qbo_webhook_at")
    print(f"ℹ️   QBO webhook events stored: {events_stored}")
    if last_webhook:
        print(f"ℹ️   Last QBO webhook received: {last_webhook}")

    # ── 2. Payment success page ─────────────────────────────────────────────
    try:
        resp2 = requests.get(f"{BASE_URL}/payment/success", timeout=15)
        all_ok &= _check("Tap redirect success page is live", resp2.status_code == 200)
    except Exception as exc:
        all_ok &= _check("Tap redirect success page is live", False, str(exc))

    # ── 3. Tap webhook endpoint sanity (unknown charge should be handled) ────
    try:
        resp3 = requests.post(
            f"{BASE_URL}/webhook/tap",
            json={"id": "chg_doesnotexist", "status": "CAPTURED", "amount": 1, "currency": "KWD"},
            timeout=15,
        )
        all_ok &= _check(
            "Tap webhook handler responds safely to unknown charges",
            resp3.status_code in (200, 404, 500),
            f"status={resp3.status_code}",
        )
    except Exception as exc:
        all_ok &= _check("Tap webhook handler reachable", False, str(exc))

    print()
    if not qbo_ok:
        print("⚠️  ACTION NEEDED: QBO tokens are invalid or expired.")
        print("   Run on your Mac (Cursor terminal):")
        print("     cd ~/Documents/prime-automation-hub")
        print("     source .venv/bin/activate")
        print("     python -m scripts.authorize")
        print("     python -m scripts.encode_tokens_for_render")
        print("   Then paste the output into Render → Environment → QBO_TOKENS_JSON → Save.")
        print()

    if events_stored == 0:
        print("⚠️  No QBO webhook events stored — invoice automation cannot start.")
        print("   Intuit must POST to one of these URLs (Production → Webhooks):")
        for url in data.get("expected_qbo_webhook_urls") or [
            "https://prime-automation-hub.onrender.com/webhook",
            "https://prime-qbo-webhook.netlify.app/quickbooks-webhook",
        ]:
            print(f"     • {url}")
        print()
        print("   Also verify on Render:")
        print("     • QBO_WEBHOOK_VERIFIER_TOKEN = Production verifier from Intuit (NOT Development)")
        print("     • Invoice entity subscribed in Intuit webhook settings")
        print("   Check Render logs for qbo_webhook_invalid_signature (401 = token mismatch).")
        print("   After fixing, create a test invoice OR recover manually:")
        print("     curl -X POST https://prime-automation-hub.onrender.com/admin/process-invoice/DOC# \\")
        print("       -H 'X-Admin-Token: <your QBO_WEBHOOK_VERIFIER_TOKEN>'")
        print()

    print("=" * 60)
    if all_ok and qbo_ok and events_stored > 0:
        print("🎉  Everything looks healthy. Safe to create new invoices.")
    elif all_ok and qbo_ok:
        print("🔧  Hub is up but QBO webhooks are not arriving — fix Intuit URL / verifier token.")
    else:
        print("🔧  One or more checks failed — see above for what to fix.")
    print("=" * 60)


if __name__ == "__main__":
    main()
