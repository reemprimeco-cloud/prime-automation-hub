"""Send a signed test QBO webhook to the live hub (or localhost).

Proves the full path: signature verify → store event → queue invoice processing.
Run on Render Shell after setting QBO_WEBHOOK_VERIFIER_TOKEN:

    python3 -m scripts.test_qbo_webhook

Then check /health — qbo_webhook_events_stored should increase by 1.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from webhook.verify import compute_signature

HUB_URL = os.getenv("HUB_URL", "https://prime-automation-hub.onrender.com").rstrip("/")

# CloudEvents shape (what Intuit sends today) + legacy is also tested in unit tests.
TEST_PAYLOAD = {
    "specversion": "1.0",
    "id": "prime-automation-hub-self-test",
    "source": "prime-automation-hub.test",
    "type": "qbo.invoice.created.v1",
    "datacontenttype": "application/json",
    "time": "2026-06-17T12:00:00Z",
    "intuitentityid": "self-test-invoice-id",
    "intuitaccountid": os.getenv("QBO_REALM_ID", "9130357945907536"),
    "data": {
        "entityName": "Invoice",
        "entityId": "self-test-invoice-id",
        "operation": "Create",
        "lastUpdated": "2026-06-17T12:00:00Z",
    },
}


def main() -> None:
    token = os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip()
    if not token:
        print("✗  QBO_WEBHOOK_VERIFIER_TOKEN is not set")
        raise SystemExit(1)

    body = json.dumps(TEST_PAYLOAD, separators=(",", ":")).encode()
    signature, _ = compute_signature(body, token)

    req = urllib.request.Request(
        f"{HUB_URL}/webhook",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "intuit-signature": signature,
        },
    )

    print(f"POST {HUB_URL}/webhook")
    print(f"  verifier token length: {len(token)}")
    print(f"  payload bytes: {len(body)}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            print(f"  HTTP {resp.status}")
            print(f"  body: {text}")
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code}")
        print(f"  body: {exc.read().decode()}")
        if exc.code == 401:
            print(
                "\n✗  Signature rejected — Render QBO_WEBHOOK_VERIFIER_TOKEN does not match\n"
                "   Intuit Production → Webhooks → verifier token. Redeploy after fixing."
            )
        raise SystemExit(1) from exc

    print(
        "\n✓  Signed webhook accepted. Check:\n"
        f"   curl {HUB_URL}/health\n"
        "   Expect qbo_webhook_events_stored >= 1"
    )


if __name__ == "__main__":
    main()
