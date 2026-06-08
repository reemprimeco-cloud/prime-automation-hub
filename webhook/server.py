"""Prime Automation Hub — webhook server.

Endpoints:
  POST /webhook      — QBO change notifications (invoice created, etc.)
  POST /webhook/tap  — Tap payment capture notifications
  GET  /health       — liveness + config status

Start:  python -m scripts.start_webhook
"""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, Header, HTTPException, Request, status

from logging_config import get_logger
from webhook.storage import count_all_events, get_recent_events, store_webhook_payload
from webhook.verify import verify_signature

_LOG = get_logger("webhook.server")

app = FastAPI(title="Prime Automation Hub", version="1.0.0")


# ── QBO webhook ───────────────────────────────────────────────────────────────

@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_qbo_webhook(
    request: Request,
    intuit_signature: str | None = Header(default=None, alias="intuit-signature"),
) -> dict:
    payload_bytes: bytes = await request.body()
    verifier_token = os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip()

    if not verifier_token:
        raise HTTPException(status_code=500, detail="QBO_WEBHOOK_VERIFIER_TOKEN not set")

    if not verify_signature(payload_bytes, intuit_signature or "", verifier_token):
        _LOG.warning("qbo_webhook_invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid intuit-signature")

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    events_stored = store_webhook_payload(payload, payload_bytes.decode("utf-8"))
    _LOG.info("qbo_webhook_received", extra={"events_stored": events_stored})
    return {"status": "ok", "events_stored": events_stored}


# ── Tap webhook ───────────────────────────────────────────────────────────────

@app.post("/webhook/tap", status_code=status.HTTP_200_OK)
async def receive_tap_webhook(request: Request) -> dict:
    """Handle Tap charge.captured — marks QBO invoice paid + sends WhatsApp."""
    payload_bytes: bytes = await request.body()

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    charge_id     = payload.get("id", "")
    charge_status = payload.get("status", "")
    amount        = float(payload.get("amount", 0))
    currency      = payload.get("currency", "KWD")

    _LOG.info("tap_webhook_received",
              extra={"charge_id": charge_id, "status": charge_status})

    # Ignore everything except captured payments
    if charge_status != "CAPTURED":
        return {"status": "ignored", "charge_status": charge_status}

    if not charge_id:
        raise HTTPException(status_code=400, detail="Missing charge id")

    try:
        from config import get_settings
        from qbo.client import QuickBooksClient
        from messaging.whatsapp import whatsapp_client_from_settings
        from workflows.payment_capture import (
            handle_payment_capture, CaptureResult, CaptureError
        )

        settings = get_settings()
        qbo      = QuickBooksClient(settings=settings)
        wa       = whatsapp_client_from_settings(settings)

        result = handle_payment_capture(
            charge_id, amount, currency,
            qbo_client=qbo,
            whatsapp_client=wa,
        )

        if isinstance(result, CaptureError):
            if result.reason == "CHARGE_NOT_FOUND":
                return {"status": "unknown_charge"}
            raise HTTPException(status_code=500, detail=result.detail)

        return {
            "status": "ok",
            "capture_status": result.status,
            "invoice_id": result.invoice_id,
            "qbo_payment_id": result.qbo_payment_id,
            "whatsapp_sent": result.whatsapp_sent,
        }

    except HTTPException:
        raise
    except Exception as exc:
        _LOG.error("tap_webhook_error", extra={"error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc))


# ── health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "qbo_token_configured": bool(os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "")),
        "tap_events_stored": count_all_events(),
        "recent_events": get_recent_events(limit=5),
    }
