"""Prime Automation Hub — webhook server.

Endpoints:
  POST /webhook         — QBO change notifications (auto-processes Invoice Create)
  POST /webhook/tap     — Tap payment capture notifications
  GET  /payment/success — customer lands here after Tap payment
  GET  /invoice/{id}/pdf — QBO invoice PDF proxy
  POST /webhook/whatsapp — inbound admin WhatsApp button actions
  GET  /health          — liveness + config status
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, Response

from db.payment_links import bootstrap_links_from_file, init_table as init_payment_links_table
from logging_config import get_logger
from webhook.storage import count_all_events, get_recent_events, init_db, store_webhook_payload
from webhook.verify import verify_signature

_LOG = get_logger("webhook.server")

app = FastAPI(title="Prime Automation Hub", version="1.0.0")

_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
_PAYMENT_SUCCESS_PAGE = _PUBLIC_DIR / "payment" / "success.html"


@app.on_event("startup")
async def _startup() -> None:
    """Ensure SQLite tables exist before webhooks or background tasks run."""
    init_payment_links_table()
    init_db()
    bootstrap_links_from_file()
    if os.getenv("QBO_TOKENS_JSON", "").strip():
        from config import get_settings
        from auth.token_store import bootstrap_from_env

        settings = get_settings()
        if bootstrap_from_env(settings.token_path, force=True):
            _LOG.info("qbo_tokens_bootstrapped_from_env")
        else:
            _LOG.error(
                "qbo_tokens_bootstrap_skipped",
                extra={"token_path": settings.token_path},
            )
    _LOG.info("database_ready")


# ── QBO invoice processor (background task) ───────────────────────────────────

def _process_qbo_invoice(invoice_id: str) -> None:
    """Run the full invoice → Tap link → WhatsApp workflow in the background.

    Called when QBO fires an Invoice Create event. Runs after the webhook
    has already returned 200 to Intuit, so processing time doesn't matter.
    """
    try:
        from config import get_settings
        from qbo.client import QuickBooksClient
        from tap.client import tap_client_from_settings
        from messaging.whatsapp import whatsapp_client_from_settings
        from workflows.invoice_to_tap import process_invoice, LinkResult, SkipResult

        settings  = get_settings()
        qbo       = QuickBooksClient(settings=settings)
        tap       = tap_client_from_settings(settings)
        whatsapp  = whatsapp_client_from_settings(settings)

        result = process_invoice(
            invoice_id,
            qbo_client=qbo,
            tap_client=tap,
            whatsapp_client=whatsapp,
            settings=settings,
        )

        if isinstance(result, SkipResult):
            _LOG.info("qbo_invoice_skipped",
                      extra={"invoice_id": invoice_id, "reason": result.reason})
        elif isinstance(result, LinkResult):
            _LOG.info("qbo_invoice_processed",
                      extra={
                          "invoice_id": invoice_id,
                          "tap_charge_id": result.tap_charge_id,
                          "whatsapp_sent": result.whatsapp_sent,
                          "status": result.status,
                      })
    except Exception as exc:
        _LOG.error(
            "qbo_invoice_process_error",
            extra={
                "invoice_id": invoice_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )


# ── QBO webhook ───────────────────────────────────────────────────────────────

@app.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_qbo_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    intuit_signature: str | None = Header(default=None, alias="intuit-signature"),
) -> dict:
    """Receive QBO change notification, verify, store, and process Invoice Creates."""
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

    # ── auto-process Invoice Create / Update (no link yet) ────────────────────
    invoice_ids_to_process: list[str] = []
    for notification in payload.get("eventNotifications", []):
        entities = notification.get("dataChangeEvent", {}).get("entities", [])
        for entity in entities:
            if entity.get("name") != "Invoice":
                continue
            operation = entity.get("operation", "")
            invoice_id = str(entity.get("id", ""))
            if not invoice_id:
                continue
            if operation == "Create":
                invoice_ids_to_process.append(invoice_id)
            elif operation == "Update":
                from db import payment_links as db

                db.init_table()
                if db.get_by_invoice_id(invoice_id) is None:
                    invoice_ids_to_process.append(invoice_id)

    for invoice_id in invoice_ids_to_process:
        _LOG.info("qbo_invoice_create_detected", extra={"invoice_id": invoice_id})
        background_tasks.add_task(_process_qbo_invoice, invoice_id)

    _LOG.info("qbo_webhook_received",
              extra={"events_stored": events_stored,
                     "invoices_queued": len(invoice_ids_to_process)})

    return {
        "status": "ok",
        "events_stored": events_stored,
        "invoices_queued": len(invoice_ids_to_process),
    }


# ── Tap webhook ───────────────────────────────────────────────────────────────

@app.post("/webhook/tap", status_code=status.HTTP_200_OK)
async def receive_tap_webhook(request: Request) -> dict:
    """Handle Tap charge.captured — marks QBO invoice paid + sends WhatsApp."""
    payload_bytes: bytes = await request.body()

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    tap_id = payload.get("id", "")
    tap_object = payload.get("object", "")

    if tap_id.startswith("inv_") or tap_object == "invoice":
        tap_status = payload.get("status", "")
        amount = float(payload.get("amount", 0))
        currency = payload.get("currency", "KWD")

        _LOG.info(
            "tap_invoice_webhook_received",
            extra={"invoice_id": tap_id, "status": tap_status},
        )

        if tap_status != "PAID":
            return {"status": "ignored", "tap_status": tap_status}

        if not tap_id:
            raise HTTPException(status_code=400, detail="Missing invoice id")

        capture_ref = tap_id
        transactions = payload.get("transactions") or []
        if transactions:
            capture_ref = str(transactions[0].get("id") or tap_id)
    else:
        charge_id = tap_id
        charge_status = payload.get("status", "")
        amount = float(payload.get("amount", 0))
        currency = payload.get("currency", "KWD")

        _LOG.info("tap_webhook_received",
                  extra={"charge_id": charge_id, "status": charge_status})

        if charge_status != "CAPTURED":
            return {"status": "ignored", "charge_status": charge_status}

        if not charge_id:
            raise HTTPException(status_code=400, detail="Missing charge id")

        capture_ref = charge_id

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
            tap_id if (tap_id.startswith("inv_") or tap_object == "invoice") else capture_ref,
            amount, currency,
            qbo_client=qbo,
            whatsapp_client=wa,
            tap_payment_ref=capture_ref,
            tap_webhook_payload=payload,
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


# ── Tap payment redirect landing page ─────────────────────────────────────────

@app.get("/payment/success")
async def payment_success() -> FileResponse:
    """Landing page after Tap redirects the customer back from checkout."""
    if not _PAYMENT_SUCCESS_PAGE.is_file():
        raise HTTPException(status_code=500, detail="Payment success page not found")
    return FileResponse(_PAYMENT_SUCCESS_PAGE, media_type="text/html; charset=utf-8")


# ── Invoice PDF proxy ──────────────────────────────────────────────────────────

@app.get("/invoice/{invoice_id}/pdf")
async def get_invoice_pdf(invoice_id: str) -> Response:
    """Serve QBO invoice PDF directly — no customer auth required."""
    from config import get_settings
    from qbo.client import QuickBooksClient, NotAuthorizedError, QBOError

    try:
        settings = get_settings()
        qbo = QuickBooksClient(settings=settings)
        pdf_bytes = qbo.get_invoice_pdf(invoice_id)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="invoice-{invoice_id}.pdf"'
            },
        )
    except NotAuthorizedError as exc:
        _LOG.error("invoice_pdf_auth_error", extra={"invoice_id": invoice_id, "error": str(exc)})
        raise HTTPException(status_code=503, detail="QuickBooks authorization required")
    except QBOError as exc:
        _LOG.error("invoice_pdf_error", extra={"invoice_id": invoice_id, "error": str(exc)})
        raise HTTPException(status_code=404, detail="Invoice PDF not found")
    except Exception as exc:
        _LOG.error("invoice_pdf_error", extra={"invoice_id": invoice_id, "error": str(exc)})
        raise HTTPException(status_code=500, detail="Failed to fetch invoice PDF")


# ── inbound WhatsApp (admin buttons) ───────────────────────────────────────────

@app.post("/webhook/whatsapp", status_code=status.HTTP_200_OK)
async def receive_whatsapp_webhook(request: Request) -> dict:
    """Handle inbound WhatsApp messages from the admin (button taps)."""
    from config import get_settings
    from messaging.whatsapp import whatsapp_client_from_settings
    from qbo.client import QuickBooksClient
    from workflows.admin_whatsapp import (
        handle_admin_action,
        normalize_whatsapp_number,
        parse_button_payload,
    )

    form_bytes = await request.body()
    form = {
        key: values[0]
        for key, values in parse_qs(form_bytes.decode("utf-8")).items()
        if values
    }
    sender = normalize_whatsapp_number(str(form.get("From", "")))
    body = str(form.get("Body", ""))
    button_payload = str(form.get("ButtonPayload", "") or form.get("ButtonText", ""))

    settings = get_settings()
    admin_phone = normalize_whatsapp_number(settings.twilio_admin_phone)
    if not admin_phone:
        raise HTTPException(status_code=500, detail="TWILIO_ADMIN_PHONE not set")
    if sender != admin_phone:
        _LOG.warning("whatsapp_inbound_rejected", extra={"from": sender})
        raise HTTPException(status_code=403, detail="Unauthorized sender")

    parsed = parse_button_payload(body, button_payload)
    if parsed is None:
        _LOG.info("whatsapp_inbound_ignored", extra={"body": body[:80]})
        return {"status": "ignored", "detail": "No recognized button action"}

    action, doc_number = parsed
    _LOG.info(
        "whatsapp_admin_action",
        extra={"action": action, "doc_number": doc_number, "from": sender},
    )

    whatsapp = whatsapp_client_from_settings(settings)
    qbo = QuickBooksClient(settings=settings)
    result = handle_admin_action(
        action,
        doc_number,
        qbo_client=qbo,
        whatsapp_client=whatsapp,
        settings=settings,
    )

    if whatsapp is not None:
        whatsapp.send_text(admin_phone, result.reply)

    return {
        "status": "ok" if result.ok else "error",
        "action": action,
        "doc_number": doc_number,
        "reply": result.reply,
    }


# ── admin: manual invoice processing ───────────────────────────────────────────

@app.post("/admin/process-invoice/{doc_number}", status_code=status.HTTP_200_OK)
async def admin_process_invoice(
    doc_number: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Manually queue Tap link generation for an invoice (missed webhook recovery)."""
    verifier_token = os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip()
    admin_token = request.headers.get("X-Admin-Token", "").strip()
    if not verifier_token or admin_token != verifier_token:
        raise HTTPException(status_code=403, detail="Unauthorized")

    from qbo.client import QuickBooksClient
    from config import get_settings

    qbo = QuickBooksClient(settings=get_settings())
    invoice = qbo.find_invoice_by_doc_number(doc_number.strip().lstrip("#"))
    if invoice is None:
        raise HTTPException(status_code=404, detail=f"Invoice #{doc_number} not found")

    invoice_id = str(invoice.get("Id", ""))
    balance = float(invoice.get("Balance", 0))
    if balance <= 0:
        return {"status": "skipped", "reason": "ALREADY_PAID", "invoice_id": invoice_id}

    background_tasks.add_task(_process_qbo_invoice, invoice_id)
    _LOG.info("admin_process_invoice_queued", extra={"invoice_id": invoice_id, "doc_number": doc_number})
    return {
        "status": "queued",
        "invoice_id": invoice_id,
        "doc_number": doc_number,
    }


# ── health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "qbo_token_configured": bool(os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "")),
        "tap_events_stored": count_all_events(),
        "recent_events": get_recent_events(limit=5),
    }
