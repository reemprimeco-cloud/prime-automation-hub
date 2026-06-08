"""Start the QuickBooks webhook receiver.

    python -m scripts.start_webhook

Prerequisites
-------------
1. Set QBO_WEBHOOK_VERIFIER_TOKEN in .env (copy from the Intuit Developer portal:
   your app → Webhooks → Verifier Token).
2. Optionally set WEBHOOK_PORT (default: 8080).

Local HTTPS tunnel (required by Intuit)
----------------------------------------
Intuit will only deliver webhooks to an HTTPS URL. For local development, expose
the server through ngrok:

    ngrok http 8080

Then register the HTTPS forwarding URL (e.g. https://abc123.ngrok.io/webhook) in
the Intuit Developer portal → your app → Webhooks.

Webhook events are stored in webhook_events.db (configurable via WEBHOOK_DB_PATH).
"""
from __future__ import annotations

import os

import uvicorn

from webhook.server import app  # noqa: F401 — import triggers FastAPI setup


def main() -> None:
    port = int(os.getenv("WEBHOOK_PORT", "8080"))
    host = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    verifier_set = bool(os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip())

    if not verifier_set:
        print(
            "Warning: QBO_WEBHOOK_VERIFIER_TOKEN is not set. "
            "All incoming webhook requests will be rejected."
        )

    print(f"Starting webhook receiver on http://{host}:{port}/webhook")
    print(f"Health check: http://{host}:{port}/health")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
