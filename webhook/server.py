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
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, Response

from db.payment_links import bootstrap_links_from_file, init_table as init_payment_links_table
from logging_config import get_logger
from webhook.payload import parse_qbo_webhook_entities, payload_format_hint
from webhook.storage import (
    count_all_events,
    get_last_webhook_received_at,
    get_recent_events,
    init_db,
    store_webhook_payload,
)
from webhook.verify import verify_signature

_LOG = get_logger("webhook.server")

app = FastAPI(title="Prime Automation Hub", version="1.0.0")

_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
_PAYMENT_SUCCESS_PAGE = _PUBLIC_DIR / "payment" / "success.html"
_OAUTH_CALLBACK_PAGE = _PUBLIC_DIR / "oauth" / "callback.html"


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
        if bootstrap_from_env(settings.token_path, force=False):
            _LOG.info("qbo_tokens_bootstrapped_from_env")
        elif not os.path.exists(settings.token_path):
            _LOG.error(
                "qbo_tokens_missing",
                extra={"token_path": settings.token_path},
            )
    _LOG.info(
        "database_ready",
        extra={
            "qbo_webhook_url_render": "https://prime-automation-hub.onrender.com/webhook",
            "qbo_webhook_url_netlify_proxy": (
                "https://prime-qbo-webhook.netlify.app/quickbooks-webhook"
            ),
        },
    )


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
    intuit_signature: Optional[str] = Header(default=None, alias="intuit-signature"),
) -> dict:
    """Receive QBO change notification, verify, store, and process Invoice Creates."""
    payload_bytes: bytes = await request.body()
    verifier_token = os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip()

    if not verifier_token:
        raise HTTPException(status_code=500, detail="QBO_WEBHOOK_VERIFIER_TOKEN not set")

    _LOG.info("webhook_debug", extra={
        "signature_received": intuit_signature or "",
        "signature_length": len(intuit_signature or ""),
        "payload_length": len(payload_bytes),
    })
    _LOG.warning(f"qbo_signature_debug received={intuit_signature!r} token_len={len(verifier_token)}")

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
    for entity in parse_qbo_webhook_entities(payload):
        if entity.get("entity_type") != "Invoice":
            continue
        operation = entity.get("operation", "")
        invoice_id = str(entity.get("entity_id", ""))
        if not invoice_id:
            continue
        if operation == "Create":
            invoice_ids_to_process.append(invoice_id)
        elif operation == "Update":
            from db import payment_links as db

            db.init_table()
            if db.get_by_invoice_id(invoice_id) is None:
                invoice_ids_to_process.append(invoice_id)

    if not events_stored and payload:
        _LOG.warning(
            "qbo_webhook_unparsed_payload",
            extra={"format": payload_format_hint(payload)},
        )

    for invoice_id in invoice_ids_to_process:
        _LOG.info("qbo_invoice_create_detected", extra={"invoice_id": invoice_id})
        background_tasks.add_task(_process_qbo_invoice, invoice_id)

    _LOG.info("qbo_webhook_received",
              extra={"events_stored": events_stored,
                     "invoices_queued": len(invoice_ids_to_process),
                     "format": payload_format_hint(payload)})

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


# ── OAuth callback (hosted redirect — no localhost) ───────────────────────────

def _oauth_callback_html(*, error: str = "", callback_url: str = "", realm_id: str = "") -> str:
    safe_error = escape(error)
    safe_url = escape(callback_url)
    safe_realm = escape(realm_id)
    if error:
        base = escape(callback_url.split("?", 1)[0] if callback_url else "/oauth/callback")
        return f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;">
  <h2 style="color:#b00020">QuickBooks authorization failed</h2>
  <p><strong>Error:</strong> {safe_error}</p>
  <p>Register <code>{base}</code> under Redirect URIs on your Intuit app (Production tab).</p>
</body></html>"""
    return f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.5;">
  <h2>QuickBooks authorization complete</h2>
  <p>Copy the <strong>full URL</strong> from your browser address bar and paste it into your
     terminal when <code>python -m scripts.authorize</code> asks for the callback URL.</p>
  <p style="word-break:break-all;background:#f4f4f4;padding:12px;border-radius:8px;">{safe_url}</p>
  <p><strong>realmId:</strong> {safe_realm or "(missing)"}</p>
  <p>You can close this tab after pasting the URL in the terminal.</p>
</body></html>"""


@app.get("/oauth/callback")
async def oauth_callback(request: Request) -> HTMLResponse:
    """Landing page after Intuit OAuth — copy the URL back to scripts.authorize."""
    params = dict(request.query_params)
    error = params.get("error", "")
    callback_url = str(request.url)
    if _OAUTH_CALLBACK_PAGE.is_file() and not error and params.get("code"):
        return FileResponse(_OAUTH_CALLBACK_PAGE, media_type="text/html; charset=utf-8")
    return HTMLResponse(
        _oauth_callback_html(
            error=error,
            callback_url=callback_url,
            realm_id=params.get("realmId", ""),
        )
    )


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

    from qbo.client import NotAuthorizedError, QBOError, QuickBooksClient
    from config import get_settings

    try:
        qbo = QuickBooksClient(settings=get_settings())
        invoice = qbo.find_invoice_by_doc_number(doc_number.strip().lstrip("#"))
    except NotAuthorizedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except QBOError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

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
    qbo_api_ok = False
    qbo_api_error = ""
    try:
        from config import get_settings
        from qbo.client import QuickBooksClient

        qbo = QuickBooksClient(settings=get_settings())
        qbo.get_company_info()
        qbo_api_ok = True
    except Exception as exc:
        qbo_api_error = str(exc)[:200]

    qbo_events_stored = count_all_events()
    last_qbo_webhook_at = get_last_webhook_received_at()

    return {
        "status": "healthy",
        "qbo_webhook_verifier_configured": bool(
            os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip()
        ),
        "qbo_token_configured": bool(os.getenv("QBO_TOKENS_JSON", "").strip()),
        "qbo_api_ok": qbo_api_ok,
        "qbo_api_error": qbo_api_error or None,
        "qbo_webhook_events_stored": qbo_events_stored,
        "last_qbo_webhook_at": last_qbo_webhook_at,
        "qbo_webhooks_receiving": qbo_events_stored > 0,
        "tap_events_stored": qbo_events_stored,
        "recent_events": get_recent_events(limit=5),
        "expected_qbo_webhook_urls": [
            "https://prime-automation-hub.onrender.com/webhook",
            "https://prime-qbo-webhook.netlify.app/quickbooks-webhook",
        ],
    }
