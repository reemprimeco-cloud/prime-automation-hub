"""Prime Automation Hub — webhook server.

Endpoints:
  POST /webhook         — QBO change notifications (auto-processes Invoice Create)
  POST /webhook/tap     — Tap payment capture notifications
  POST /webhook/upayments — UPayments payment capture notifications
  GET  /payment/success — customer lands here after payment
  GET  /invoice/{id}/pdf — QBO invoice PDF proxy
  POST /webhook/whatsapp — inbound admin WhatsApp button actions
  GET  /health          — liveness + config status
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qs, quote, urlencode

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from auth.token_store import force_bootstrap_requested
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

# In-memory delivery counters for the QBO webhook, reset on restart.
# The stored-event count alone cannot explain a silent endpoint: a rejected
# signature returns 401 before anything is written, so "Intuit never called us"
# and "Intuit called us with a token we don't match" look identical. These
# separate the two.
_QBO_WEBHOOK_STATS: dict = {
    "last_request_at": None,
    "accepted": 0,
    "rejected_signature": 0,
    "last_rejected_at": None,
}

app = FastAPI(title="Prime Automation Hub", version="1.0.0")

_PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
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

_INVOICE_PROCESS_RETRY_DELAYS_SEC = (3, 10, 30)


def _should_retry_invoice_process(exc: Exception) -> bool:
    """Retry when QBO may not have the new invoice readable yet (webhook race)."""
    from qbo.client import QBOError

    if not isinstance(exc, QBOError):
        return False
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("404", "not found", "object not found", "business validation")
    )


def _handle_invoice_update(invoice_id: str, existing_link: dict) -> None:
    try:
        from config import get_settings
        from db import payment_links as db
        from qbo.client import QuickBooksClient

        settings = get_settings()
        qbo = QuickBooksClient(settings=settings)
        invoice = qbo.get_invoice(invoice_id)
        total = float(invoice.get("TotalAmt", 0))
        old_amount = float(existing_link.get("amount", 0))
        balance_due = float(invoice.get("Balance", 0))
        partially_paid = balance_due < total - 0.001
        link_covers_more_than_due = old_amount > balance_due + 0.001

        if balance_due <= 0:
            _LOG.info(
                "qbo_invoice_update_already_paid",
                extra={"invoice_id": invoice_id},
            )
            return
        if (
            abs(balance_due - old_amount) < 0.001
            and not (partially_paid and link_covers_more_than_due)
        ):
            _LOG.info(
                "qbo_invoice_update_amount_unchanged",
                extra={
                    "invoice_id": invoice_id,
                    "amount": old_amount,
                    "balance_due": balance_due,
                    "total": total,
                },
            )
            return
        _LOG.info(
            "qbo_invoice_update_amount_changed",
            extra={
                "invoice_id": invoice_id,
                "old_amount": old_amount,
                "new_amount": balance_due,
                "total": total,
                "partially_paid": partially_paid,
            },
        )
        db.delete_by_invoice_id(invoice_id)
        _process_qbo_invoice(invoice_id)
    except Exception as exc:
        _LOG.error(
            "qbo_invoice_update_check_error",
            extra={
                "invoice_id": invoice_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )


def _process_qbo_invoice(invoice_id: str) -> None:
    """Run the full invoice → UPayments link → WhatsApp workflow in the background.

    Called when QBO fires an Invoice Create event. Runs after the webhook
    has already returned 200 to Intuit, so processing time doesn't matter.
    Retries with backoff when QBO has not propagated the invoice yet.
    """
    from qbo.client import QBOError, QuickBooksClient
    from upayments.client import upayments_client_from_env
    from messaging.whatsapp import whatsapp_client_from_settings
    from workflows.invoice_to_tap import process_invoice, LinkResult, SkipResult

    max_attempts = len(_INVOICE_PROCESS_RETRY_DELAYS_SEC) + 1
    for attempt in range(max_attempts):
        if attempt > 0:
            delay = _INVOICE_PROCESS_RETRY_DELAYS_SEC[attempt - 1]
            time.sleep(delay)

        try:
            from config import get_settings

            settings = get_settings()
            qbo = QuickBooksClient(settings=settings)
            upayments = upayments_client_from_env()
            whatsapp = whatsapp_client_from_settings(settings)

            result = process_invoice(
                invoice_id,
                qbo_client=qbo,
                upayments_client=upayments,
                whatsapp_client=whatsapp,
                settings=settings,
            )

            if isinstance(result, SkipResult):
                _LOG.info(
                    "qbo_invoice_skipped",
                    extra={"invoice_id": invoice_id, "reason": result.reason},
                )
                return

            if isinstance(result, LinkResult):
                _LOG.info(
                    "qbo_invoice_processed",
                    extra={
                        "invoice_id": invoice_id,
                        "tap_charge_id": result.tap_charge_id,
                        "whatsapp_sent": result.whatsapp_sent,
                        "status": result.status,
                    },
                )
                return

        except QBOError as exc:
            if attempt + 1 < max_attempts and _should_retry_invoice_process(exc):
                _LOG.warning(
                    "qbo_invoice_process_retry",
                    extra={
                        "invoice_id": invoice_id,
                        "attempt": attempt + 1,
                        "error": str(exc)[:200],
                    },
                )
                continue
            _LOG.error(
                "qbo_invoice_process_error",
                extra={
                    "invoice_id": invoice_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return
        except Exception as exc:
            _LOG.error(
                "qbo_invoice_process_error",
                extra={
                    "invoice_id": invoice_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return


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
    _QBO_WEBHOOK_STATS["last_request_at"] = datetime.now(timezone.utc).isoformat()

    if not verifier_token:
        raise HTTPException(status_code=500, detail="QBO_WEBHOOK_VERIFIER_TOKEN not set")

    if not verify_signature(payload_bytes, intuit_signature or "", verifier_token):
        _QBO_WEBHOOK_STATS["rejected_signature"] += 1
        _QBO_WEBHOOK_STATS["last_rejected_at"] = _QBO_WEBHOOK_STATS["last_request_at"]
        _LOG.warning(
            "qbo_webhook_invalid_signature",
            extra={"signature_present": bool((intuit_signature or "").strip())},
        )
        raise HTTPException(status_code=401, detail="Invalid intuit-signature")

    _QBO_WEBHOOK_STATS["accepted"] += 1

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    events_stored = store_webhook_payload(payload, payload_bytes.decode("utf-8"))
    parsed_entities = parse_qbo_webhook_entities(payload)

    # ── auto-process Invoice Create / Update (no link yet) ────────────────────
    invoice_ids_to_process: list[str] = []
    for entity in parsed_entities:
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
            existing_link = db.get_by_invoice_id(invoice_id)
            if existing_link is None:
                invoice_ids_to_process.append(invoice_id)
            else:
                background_tasks.add_task(
                    _handle_invoice_update, invoice_id, existing_link
                )

    if not events_stored and payload:
        _LOG.warning(
            "qbo_webhook_unparsed_payload",
            extra={"format": payload_format_hint(payload)},
        )

    for invoice_id in dict.fromkeys(invoice_ids_to_process):
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


# ── UPayments webhook ─────────────────────────────────────────────────────────

@app.post("/webhook/upayments", status_code=status.HTTP_200_OK)
async def receive_upayments_webhook(
    request: Request,
    x_notification_token: Optional[str] = Header(default=None),
    x_signature: Optional[str] = Header(default=None),
) -> dict:
    """Handle UPayments notification — marks QBO invoice paid + sends WhatsApp.

    Live UPayments posts ``application/x-www-form-urlencoded`` with
    ``result=CAPTURED`` and ``x-notification-token``.
    """
    payload_bytes: bytes = await request.body()
    content_type = (request.headers.get("content-type") or "").lower()

    payload: dict
    if "application/json" in content_type:
        try:
            parsed = json.loads(payload_bytes or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        payload = parsed if isinstance(parsed, dict) else {}
    else:
        # Default / observed live: form-urlencoded
        payload = {
            key: (values[0] if values else "")
            for key, values in parse_qs(
                payload_bytes.decode("utf-8", errors="replace"),
                keep_blank_values=True,
            ).items()
        }

    from upayments.client import upayments_client_from_env
    from upayments.exceptions import UPaymentsAPIError

    try:
        up = upayments_client_from_env(webhook_only=True)
    except UPaymentsAPIError as exc:
        _LOG.error("upayments_webhook_client_config_error", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail="UPayments not configured") from exc

    token_header = (x_notification_token or x_signature or "").strip()
    if not up.verify_webhook_signature(payload_bytes, token_header):
        _LOG.warning("upayments_webhook_auth_failed")
        raise HTTPException(status_code=401, detail="Invalid notification token")

    try:
        event = up.parse_webhook_event(payload)
    except UPaymentsAPIError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _LOG.info(
        "upayments_webhook_received",
        extra={
            "result": payload.get("result"),
            "track_id": event.provider_transaction_id,
            "invoice_number": event.invoice_number,
            "status": event.status,
        },
    )

    if event.status != "paid":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        track_id = str(data.get("track_id") or "").strip()
        if track_id and event.invoice_number:
            from db import payment_links as db

            db.record_payment_attempt(event.invoice_number, track_id)
        return {
            "status": "ignored",
            "result": payload.get("result"),
            "provider_status": event.status,
        }

    if not event.provider_transaction_id and not event.invoice_number:
        raise HTTPException(status_code=400, detail="Missing track_id / order id")

    amount = float(event.amount) if event.amount is not None else 0.0
    currency = event.currency or "KWD"
    capture_ref = event.provider_transaction_id or event.invoice_number

    try:
        from config import get_settings
        from qbo.client import QuickBooksClient
        from messaging.whatsapp import whatsapp_client_from_settings
        from workflows.payment_capture import handle_payment_capture, CaptureError

        settings = get_settings()
        qbo = QuickBooksClient(settings=settings)
        wa = whatsapp_client_from_settings(settings)

        result = handle_payment_capture(
            capture_ref,
            amount,
            currency,
            qbo_client=qbo,
            whatsapp_client=wa,
            tap_payment_ref=str(
                payload.get("payment_id") or payload.get("tran_id") or capture_ref
            ),
            tap_webhook_payload=payload,
        )

        if isinstance(result, CaptureError):
            if result.reason == "CHARGE_NOT_FOUND":
                return {
                    "status": "unknown_charge",
                    "track_id": capture_ref,
                    "invoice_number": event.invoice_number,
                }
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
        _LOG.error("upayments_webhook_error", extra={"error": str(exc)})
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


# ── Payment redirect landing page (UPayments returnUrl / cancelUrl) ────────────
# Both UPAYMENTS_RETURN_URL and UPAYMENTS_CANCEL_URL may point here. Outcome is
# NEVER inferred from which URL redirected — only from get_charge_status.

_PAID_REDIRECT_RESULTS = frozenset({"CAPTURED", "PAID", "SUCCESS"})
_FAILED_REDIRECT_RESULTS = frozenset({
    "NOT CAPTURED",
    "CANCELED",
    "CANCELLED",
    "FAILED",
    "DECLINED",
    "ERROR",
    "VOIDED",
})
_SUPPORT_WHATSAPP_URL = "https://wa.me/96565068000"


def _payment_result_from_status_payload(status_resp: dict) -> str:
    """Extract gateway ``result`` from get-payment-status JSON (best-effort)."""
    data = status_resp.get("data") if isinstance(status_resp.get("data"), dict) else status_resp
    if not isinstance(data, dict):
        return ""
    transaction = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    return str(
        transaction.get("result")
        or data.get("result")
        or data.get("payment_status")
        or data.get("status")
        or status_resp.get("result")
        or ""
    ).strip()


_LOGO_FILE = _PUBLIC_DIR / "brand" / "prime-logo-navy.png"

# GE Dinar Two is self-hosted once the licensed files are dropped into
# public/brand/fonts/ (woff2 preferred); until then the page falls back to
# IBM Plex Sans Arabic from Google Fonts.
_FONT_DIR = _PUBLIC_DIR / "brand" / "fonts"
_ARABIC_BRAND_FONTS = {400: "ge-dinar-two-regular", 500: "ge-dinar-two-medium"}
_FONT_FORMATS = {".woff2": "woff2", ".woff": "woff", ".otf": "opentype", ".ttf": "truetype"}

PaymentState = Literal[
    "success",      # verified paid, arriving from the gateway
    "paid",         # verified paid, reopened from the /pay link
    "failed",       # verified not paid
    "retry_error",  # verified not paid, and we could not start a fresh attempt
    "unknown",      # we have a charge id but no final answer yet
    "unreadable",   # we were given nothing to check
    "inactive",     # unknown /pay link
]

# Status re-checks on the "confirming" page: every 8s, at most 5 times.
_RECHECK_SECONDS = 8
_RECHECK_LIMIT = 5

_ICON_PATHS = {
    "check": '<circle cx="12" cy="12" r="10"/><path class="draw" d="m7.5 12.5 3 3 6-6.5"/>',
    "cross": '<circle cx="12" cy="12" r="10"/><path d="m9 9 6 6m0-6-6 6"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 7v5l3.2 2"/>',
    "question": '<circle cx="12" cy="12" r="10"/><path d="M9.6 9.4a2.5 2.5 0 0 1 4.8 1c0 1.7-2.4 2.1-2.4 3.6"/><path d="M12 17.2h.01"/>',
    "renew": '<circle cx="12" cy="12" r="10"/><path d="M16 12a4 4 0 1 1-1.2-2.85"/><path d="M15.2 6.6v2.9h-2.9"/>',
}
_STATE_ICON = {
    "success": "check", "paid": "check", "failed": "cross", "retry_error": "cross",
    "unknown": "clock", "unreadable": "question", "inactive": "renew",
}
_STATE_TONE = {
    "success": "ok", "paid": "ok", "failed": "warn", "retry_error": "warn",
    "unknown": "info", "unreadable": "info", "inactive": "muted",
}

# (EN heading, EN lead, EN rest, AR heading, AR lead, AR rest). The lead is
# the sentence the customer most needs, so it's set in full ink.
_STATE_COPY = {
    "success": (
        "Payment successful",
        "Thank you, we've received your payment.",
        "A confirmation is on its way to you on WhatsApp.",
        "تمت عملية الدفع بنجاح",
        "شكراً لك، تم استلام دفعتك.",
        "ستصلك رسالة تأكيد عبر واتساب.",
    ),
    "paid": (
        "This invoice is paid",
        "Thank you, we've received your payment.",
        "There's nothing more to do on this link.",
        "تم دفع هذه الفاتورة",
        "شكراً لك، تم استلام دفعتك.",
        "لا حاجة لأي إجراء آخر على هذا الرابط.",
    ),
    "failed": (
        "Payment not completed",
        "You have not been charged.",
        "You can try again, or message us if you need help.",
        "لم تكتمل عملية الدفع",
        "لم يتم خصم أي مبلغ.",
        "يمكنك المحاولة مرة أخرى، أو التواصل معنا إن احتجت إلى مساعدة.",
    ),
    "retry_error": (
        "We couldn't start a new attempt",
        "You have not been charged.",
        "Please try again in a minute, or message us and we'll help.",
        "تعذّر بدء محاولة دفع جديدة",
        "لم يتم خصم أي مبلغ.",
        "يرجى المحاولة بعد دقيقة، أو التواصل معنا وسنساعدك.",
    ),
    "unknown": (
        "Confirming your payment",
        "This can take a minute.",
        "Once it's confirmed we'll message you on WhatsApp. "
        "If you don't hear from us within 10 minutes, please message us.",
        "جارٍ تأكيد عملية الدفع",
        "قد يستغرق ذلك دقيقة.",
        "سنرسل لك رسالة عبر واتساب فور التأكيد، وإن لم تصلك خلال 10 دقائق يرجى التواصل معنا.",
    ),
    "unreadable": (
        "We couldn't read your payment result",
        "If you completed the payment, a WhatsApp confirmation will reach you shortly.",
        "If it doesn't, open your payment link again or message us.",
        "تعذّرت قراءة نتيجة الدفع",
        "إذا أكملت عملية الدفع فستصلك رسالة تأكيد عبر واتساب قريباً.",
        "وإن لم تصلك، افتح رابط الدفع مرة أخرى أو تواصل معنا.",
    ),
    "inactive": (
        "This payment link is no longer active",
        "This link has been replaced or has expired.",
        "Please use the most recent payment link we sent you on WhatsApp, or message us and we'll help.",
        "رابط الدفع هذا لم يعد صالحاً",
        "تم استبدال هذا الرابط أو انتهت صلاحيته.",
        "يرجى استخدام أحدث رابط دفع أرسلناه لك عبر واتساب، أو التواصل معنا وسنساعدك.",
    ),
}

# What the prefilled WhatsApp message is about, per state.
_SUPPORT_TOPIC = {
    "success": ("payment confirmation", "تأكيد الدفع"),
    "paid": ("payment confirmation", "تأكيد الدفع"),
    "failed": ("payment problem", "مشكلة في الدفع"),
    "retry_error": ("payment problem", "مشكلة في الدفع"),
    "unknown": ("payment status", "حالة الدفع"),
    "unreadable": ("payment status", "حالة الدفع"),
    "inactive": ("payment link", "رابط الدفع"),
}

# Values from primekw.net's assets/css/tokens.css, so these pages match the site.
_PAYMENT_PAGE_CSS = """
:root {
  --navy: #10254A; --navy-deep: #08132A; --sky: #7CA5C4; --sky-ink: #3F6F96;
  --paper: #FFFFFF; --mist: #F4F7F9;
  --line: rgba(16, 37, 74, .12); --line-strong: rgba(16, 37, 74, .22);
  --ink: var(--navy-deep); --ink-mid: rgba(8, 19, 42, .64);
  --ok: #1D7A4F; --warn: #B5730C;
  --latin: 'Montserrat', system-ui, sans-serif;
  --arabic: 'GE Dinar Two', 'IBM Plex Sans Arabic', system-ui, sans-serif;
  --ease-out: cubic-bezier(.2, .7, .2, 1);
  --tone: var(--sky-ink);
  color-scheme: light;
}
.tone-ok { --tone: var(--ok); }
.tone-warn { --tone: var(--warn); }
.tone-info { --tone: var(--sky-ink); }
.tone-muted { --tone: var(--ink-mid); }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; min-height: 100vh; min-height: 100dvh; background: var(--mist);
  color: var(--ink); font: 400 15px/1.65 var(--latin);
  display: flex; flex-direction: column; align-items: center;
  padding: 32px 16px 28px;
}
::selection { background: var(--navy); color: var(--paper); }
a { color: inherit; text-underline-offset: 4px; text-decoration-thickness: 1px; }
:focus-visible { outline: 2px solid var(--sky-ink); outline-offset: 3px; border-radius: 2px; }
[lang="ar"] { font-family: var(--arabic); letter-spacing: 0; }

.brand { display: block; margin-bottom: 26px; }
.brand img { display: block; height: 44px; width: auto; }

.slip {
  width: min(100%, 560px); background: var(--paper);
  border: 1px solid var(--line); border-top: 3px solid var(--tone); border-radius: 4px;
  box-shadow: 0 22px 44px -26px rgba(16, 37, 74, .35);
}
.verdict { padding: 28px 30px 26px; }
.icon { width: 40px; height: 40px; display: block; margin-bottom: 18px; color: var(--tone); }
.icon svg { width: 100%; height: 100%; fill: none; stroke: currentColor;
  stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }

/* Arabic leads in source order; on wide screens it sits in the right-hand column. */
.copy { display: grid; grid-template-columns: 1fr 1fr; gap: 0 28px; direction: rtl; }
.copy > div { min-width: 0; }
.copy h1 { margin: 0 0 8px; font-weight: 500; font-size: 22px; line-height: 1.25;
  letter-spacing: -.01em; text-wrap: balance; }
.copy p { margin: 0; color: var(--ink-mid); max-width: 34ch; text-wrap: pretty; }
.copy strong { color: var(--ink); font-weight: 500; }
.copy [lang="ar"] h1 { font-size: 24px; line-height: 1.45; letter-spacing: 0; }
.copy [lang="ar"] p { font-size: 16px; line-height: 1.8; }

.perf { position: relative; border-top: 1px dashed var(--line-strong); }
.perf::before, .perf::after {
  content: ""; position: absolute; top: -10px; width: 20px; height: 20px;
  border-radius: 50%; background: var(--mist); border: 1px solid var(--line);
}
.perf::before { left: -11px; clip-path: inset(0 0 0 50%); }
.perf::after { right: -11px; clip-path: inset(0 50% 0 0); }

.receipt { margin: 0; padding: 18px 30px 6px; }
.row { display: grid; grid-template-columns: 1fr auto 1fr; align-items: baseline;
  gap: 12px; padding: 11px 0; border-bottom: 1px solid var(--line); }
.row:last-child { border-bottom: 0; }
.row dt { display: contents; }
.row .label { grid-row: 1; grid-column: 1; }
.row .label[lang="ar"] { grid-column: 3; }
.row dd { grid-row: 1; grid-column: 2; }
.label { font-size: 11px; font-weight: 500; letter-spacing: .16em;
  text-transform: uppercase; color: var(--ink-mid); }
.label[lang="ar"] { font-size: 13px; text-transform: none; text-align: right; }
.row dd { margin: 0; text-align: center; font-weight: 500; font-size: 16px;
  font-variant-numeric: tabular-nums lining-nums; direction: ltr; }
.row dd.ref { font-size: 13px; font-weight: 400; letter-spacing: .06em; color: var(--ink-mid); }

.actions { padding: 22px 30px 26px; display: grid; gap: 10px; }
.button, .ghost {
  display: inline-flex; align-items: center; justify-content: center; gap: 12px;
  min-height: 48px; padding: 12px 26px; border-radius: 2px; text-decoration: none;
  font-weight: 500; font-size: 13px; letter-spacing: .16em; text-transform: uppercase;
  transition: background-color .25s var(--ease-out), border-color .25s var(--ease-out);
}
.button { background: var(--navy); color: var(--paper); }
.button:hover { background: var(--navy-deep); }
.ghost { border: 1px solid var(--line-strong); color: var(--ink); }
.ghost:hover { border-color: var(--navy); }
.button [lang="ar"], .ghost [lang="ar"] { font-size: 15px; text-transform: none; }
.button > span, .ghost > span, .support > span { white-space: nowrap; }
.sep { width: 5px; height: 5px; background: var(--sky); flex: none; }

.prompt { margin: 6px 0 0; font-size: 14px; color: var(--ink-mid); text-align: center; }
.prompt > span { display: inline-block; }
.prompt [lang="ar"] { font-size: 15px; }
.support {
  display: flex; align-items: center; justify-content: center; gap: 10px; flex-wrap: wrap;
  min-height: 44px; padding: 6px 4px; font-size: 14px; font-weight: 500; color: var(--ink);
  text-decoration: underline; text-decoration-color: var(--line-strong);
}
.support:hover { text-decoration-color: currentColor; }
.support [lang="ar"] { font-size: 15px; }
.support .sep { width: 4px; height: 4px; }
.status { margin: 0; display: flex; align-items: center; justify-content: center; gap: 10px;
  font-size: 13px; color: var(--ink-mid); }
.status .pulse { width: 6px; height: 6px; background: var(--sky-ink);
  animation: pulse 1.6s var(--ease-out) infinite; }
@keyframes pulse { 0%, 100% { opacity: .25; } 50% { opacity: 1; } }

.foot { margin-top: 22px; display: flex; align-items: center; gap: 10px;
  font-size: 12px; letter-spacing: .14em; text-transform: uppercase; color: var(--ink-mid); }
.foot .mark { width: 7px; height: 7px; background: var(--sky); }
.foot a { text-decoration: none; }
.foot a:hover { color: var(--ink); }

.draw { stroke-dasharray: 20; stroke-dashoffset: 20; animation: draw .7s .15s var(--ease-out) forwards; }
@keyframes draw { to { stroke-dashoffset: 0; } }
@media (prefers-reduced-motion: reduce) {
  .draw { animation: none; stroke-dashoffset: 0; }
  .status .pulse { animation: none; opacity: 1; }
  .button, .ghost { transition: none; }
}

@media (max-width: 560px) {
  body { padding-top: 22px; }
  .brand { margin-bottom: 20px; }
  .brand img { height: 38px; }
  .verdict { padding: 24px 20px 22px; }
  .icon { margin-left: auto; }
  .copy { grid-template-columns: 1fr; gap: 18px; }
  .copy p { max-width: none; }
  .receipt { padding: 14px 20px 4px; }
  .actions { padding: 20px 20px 22px; }
  .button, .ghost { padding: 12px 16px; letter-spacing: .1em; }
  .prompt .dot { display: none; }
  .prompt > span { display: block; }
}
"""


def _arabic_font_faces() -> str:
    """@font-face rules for whichever GE Dinar Two files are present."""
    rules = []
    for weight, stem in _ARABIC_BRAND_FONTS.items():
        for ext, fmt in _FONT_FORMATS.items():
            if (_FONT_DIR / f"{stem}{ext}").is_file():
                rules.append(
                    "@font-face { font-family: 'GE Dinar Two'; font-style: normal; "
                    f"font-weight: {weight}; font-display: swap; "
                    f"src: url('/assets/fonts/{stem}{ext}') format('{fmt}'); }}"
                )
                break
    return "\n".join(rules)


def _payment_reference(status_resp: dict, charge_id: str) -> tuple[str, str, str]:
    """(EN label, AR label, value) for the receipt's reference row, or empty.

    Prefers the gateway's payment/receipt id, the number a customer can match to
    their bank. Falls back to the tail of the track id, which only support needs.
    """
    data = status_resp.get("data") if isinstance(status_resp.get("data"), dict) else status_resp
    data = data if isinstance(data, dict) else {}
    transaction = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    for key in ("payment_id", "receipt_id", "ref", "tran_id"):
        value = str(transaction.get(key) or "").strip()
        if value and value != charge_id:
            return ("Payment ID", "رقم العملية", value)
    charge_id = (charge_id or "").strip()
    if not charge_id:
        return ("", "", "")
    short = charge_id if len(charge_id) <= 12 else f"…{charge_id[-8:]}"
    return ("Reference", "رقم المرجع", short)


def _receipt_rows(record: dict | None, reference: tuple[str, str, str]) -> str:
    rows: list[tuple[str, str, str, str]] = []
    if record:
        if record.get("invoice_number"):
            rows.append(("Invoice", str(record["invoice_number"]), "رقم الفاتورة", ""))
        if record.get("amount") is not None:
            amount = f"{float(record['amount']):,.3f} {record.get('currency') or 'KWD'}"
            rows.append(("Amount", amount, "المبلغ", ""))
    ref_en, ref_ar, ref_value = reference
    if ref_value:
        rows.append((ref_en, ref_value, ref_ar, "ref"))
    if not rows:
        return ""
    body = "".join(
        f'<div class="row"><dt><span class="label">{escape(en)}</span>'
        f'<span class="label" lang="ar" dir="rtl">{escape(ar)}</span></dt>'
        f'<dd class="{cls}">{escape(value)}</dd></div>'
        for en, value, ar, cls in rows
    )
    return f'<div class="perf" aria-hidden="true"></div><dl class="receipt">{body}</dl>'


def _support_url(status: str, record: dict | None) -> str:
    """WhatsApp link with the invoice and topic already typed in."""
    topic_en, topic_ar = _SUPPORT_TOPIC[status]
    invoice = str((record or {}).get("invoice_number") or "").strip()
    if invoice:
        text = f"Hello, invoice {invoice}: {topic_en}\nمرحباً، الفاتورة {invoice}: {topic_ar}"
    else:
        text = f"Hello, about my {topic_en}\nمرحباً، بخصوص {topic_ar}"
    return f"{_SUPPORT_WHATSAPP_URL}?text={quote(text)}"


def _pair(en: str, ar: str) -> str:
    return (
        f"<span>{escape(en)}</span><span class=\"sep\" aria-hidden=\"true\"></span>"
        f'<span lang="ar" dir="rtl">{escape(ar)}</span>'
    )


def _render_payment_page(
    status: PaymentState,
    *,
    record: dict | None = None,
    reference: tuple[str, str, str] = ("", "", ""),
    check_url: str = "",
    recheck_url: str = "",
) -> HTMLResponse:
    """Customer-facing result page, bilingual, in primekw.net's brand.

    `record` is the payment_links row and `reference` the receipt reference;
    pass them only when they come from a verified status check or our own
    database, never from the redirect's query string alone. On the "unknown"
    page, `check_url` backs the "Check again" button and `recheck_url`, when
    set, makes the page re-check on its own.
    """
    en_h, en_lead, en_rest, ar_h, ar_lead, ar_rest = _STATE_COPY[status]
    support_url = escape(_support_url(status, record))
    support_link = (
        f'<a class="support" href="{support_url}">'
        f'{_pair("WhatsApp us", "تواصل معنا عبر واتساب")}</a>'
    )
    support_button = f'<a class="button" href="{support_url}">{_pair("WhatsApp us", "تواصل معنا")}</a>'
    link_id = escape(str((record or {}).get("id") or ""))
    invoice_id = str((record or {}).get("invoice_id") or "").strip()
    download = (
        f'<a class="ghost" href="/invoice/{escape(quote(invoice_id, safe=""))}/pdf">'
        f'{_pair("Download invoice", "تحميل الفاتورة")}</a>'
        if invoice_id
        else ""
    )

    if status == "success":
        actions = (
            f"{download}"
            '<p class="prompt"><span>No confirmation after a few minutes?</span> <span class="dot" aria-hidden="true">·</span> '
            '<span lang="ar" dir="rtl">لم تصلك رسالة التأكيد خلال دقائق؟</span></p>'
            f"{support_link}"
        )
    elif status == "paid":
        actions = f"{download}{support_link}"
    elif status in ("failed", "retry_error") and link_id:
        actions = (
            f'<a class="button" href="/pay/{link_id}">{_pair("Try again", "حاول مرة أخرى")}</a>'
            f"{support_link}"
        )
    elif status == "unknown":
        actions = ""
        if recheck_url:
            actions += (
                '<p class="status" role="status"><span class="pulse" aria-hidden="true"></span>'
                '<span>Checking again automatically</span> · '
                '<span lang="ar" dir="rtl">نتحقق تلقائياً</span></p>'
            )
        if check_url:
            actions += f'<a class="button" href="{escape(check_url)}">{_pair("Check again", "تحقق مرة أخرى")}</a>'
        actions += support_link
    else:
        actions = support_button

    receipt = _receipt_rows(record if status != "inactive" else None, reference)
    refresh = (
        f'<meta http-equiv="refresh" content="{_RECHECK_SECONDS};url={escape(recheck_url)}" />'
        if recheck_url
        else ""
    )
    fonts_href = (
        "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500"
        "&amp;family=Montserrat:wght@400;500&amp;display=swap"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="theme-color" content="#F4F7F9" />
    <meta name="robots" content="noindex" />
    {refresh}
    <title>{escape(en_h)} · {escape(ar_h)} — Prime Printing Co.</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link rel="stylesheet" href="{fonts_href}" media="print" onload="this.media='all'" />
    <noscript><link rel="stylesheet" href="{fonts_href}" /></noscript>
    <style>{_arabic_font_faces()}{_PAYMENT_PAGE_CSS}</style>
  </head>
  <body class="state-{status} tone-{_STATE_TONE[status]}">
    <a class="brand" href="https://primekw.net" aria-label="Prime Printing Co.">
      <img src="/assets/prime-logo-navy.png" alt="Prime Printing Co." width="720" height="315" />
    </a>
    <main class="slip">
      <section class="verdict">
        <span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24">{_ICON_PATHS[_STATE_ICON[status]]}</svg></span>
        <div class="copy">
          <div lang="ar" dir="rtl"><h1>{escape(ar_h)}</h1><p><strong>{escape(ar_lead)}</strong> {escape(ar_rest)}</p></div>
          <div dir="ltr"><h1>{escape(en_h)}</h1><p><strong>{escape(en_lead)}</strong> {escape(en_rest)}</p></div>
        </div>
      </section>
      {receipt}
      <div class="actions">{actions}</div>
    </main>
    <footer class="foot"><span class="mark" aria-hidden="true"></span>
      <a href="https://primekw.net">Prime Printing Co. · primekw.net</a></footer>
  </body>
</html>"""
    return HTMLResponse(
        html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _verified_order_number(status_resp: dict) -> str:
    """Our invoice number as echoed back in a verified get-payment-status reply."""
    data = status_resp.get("data") if isinstance(status_resp.get("data"), dict) else status_resp
    if not isinstance(data, dict):
        return ""
    transaction = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    for source in (transaction, data):
        for key in ("requested_order_id", "merchant_requested_order_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _payment_link_for(track_id: str, session_id: str, status_resp: dict) -> dict | None:
    """The payment_links row behind a verified redirect, found from trusted ids only."""
    from db import payment_links as db

    try:
        db.init_table()
        for charge_id in (track_id, session_id):
            if charge_id:
                record = db.get_by_charge_id(charge_id)
                if record:
                    return record
        order_number = _verified_order_number(status_resp)
        return db.get_by_invoice_number(order_number) if order_number else None
    except Exception as exc:
        _LOG.warning("payment_page_link_lookup_failed", extra={"error": str(exc)})
        return None


@app.get("/assets/prime-logo-navy.png")
async def prime_logo() -> FileResponse:
    return FileResponse(
        _LOGO_FILE,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/assets/fonts/{filename}")
async def brand_font(filename: str) -> Response:
    """Self-hosted GE Dinar Two files; only the known names are served."""
    stem, dot, ext = filename.rpartition(".")
    if stem not in _ARABIC_BRAND_FONTS.values() or f".{ext}" not in _FONT_FORMATS:
        return Response(status_code=404)
    path = _FONT_DIR / filename
    if not path.is_file():
        return Response(status_code=404)
    media = {"woff2": "font/woff2", "woff": "font/woff", "otf": "font/otf", "ttf": "font/ttf"}[ext]
    return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=2592000"})


def _recheck_urls(request: Request, track_id: str, session_id: str) -> tuple[str, str]:
    """("Check again" URL, automatic re-check URL or "" once the limit is reached)."""
    params = {}
    if track_id:
        params["track_id"] = track_id
    if session_id:
        params["session_id"] = session_id
    try:
        count = int(request.query_params.get("n") or 0)
    except ValueError:
        count = 0
    check_url = f"/payment/success?{urlencode(params)}"
    if count >= _RECHECK_LIMIT:
        return check_url, ""
    return check_url, f"/payment/success?{urlencode({**params, 'n': count + 1})}"


@app.get("/payment/success")
async def payment_success(request: Request) -> HTMLResponse:
    """Landing page after UPayments redirects the customer back.

    MUST verify actual payment status before showing success — never trust the
    redirect alone (customers can bookmark/reload/share this URL). returnUrl and
    cancelUrl may both point here; only get_charge_status decides the outcome.
    """
    track_id = (
        request.query_params.get("track_id")
        or request.query_params.get("trackId")
        or request.query_params.get("TrackId")
        or ""
    ).strip()
    session_id = (request.query_params.get("session_id") or "").strip()
    charge_id = track_id or session_id

    if not charge_id:
        _LOG.warning(
            "payment_success_no_identifier",
            extra={"query": dict(request.query_params)},
        )
        return _render_payment_page(status="unreadable")

    from upayments.client import upayments_client_from_env
    from upayments.exceptions import UPaymentsAPIError

    check_url, recheck_url = _recheck_urls(request, track_id, session_id)
    status_resp: dict = {}
    try:
        client = upayments_client_from_env()
        status_resp = client.get_charge_status(charge_id)
        result = _payment_result_from_status_payload(status_resp)
        result_upper = result.upper()

        if result_upper in _PAID_REDIRECT_RESULTS:
            _LOG.info(
                "payment_success_verified",
                extra={"track_id": charge_id, "result": result},
            )
            return _render_payment_page(
                status="success",
                record=_payment_link_for(track_id, session_id, status_resp),
                reference=_payment_reference(status_resp, charge_id),
            )

        if (
            result_upper in _FAILED_REDIRECT_RESULTS
            or "NOT CAPTURED" in result_upper
        ):
            _LOG.info(
                "payment_success_route_hit_but_not_paid",
                extra={"track_id": charge_id, "result": result},
            )
            return _render_payment_page(
                status="failed",
                record=_payment_link_for(track_id, session_id, status_resp),
            )

        # PENDING / empty / AUTHORIZED / unknown → never claim success or failure
        _LOG.info(
            "payment_success_status_pending_or_unknown",
            extra={"track_id": charge_id, "result": result},
        )
    except UPaymentsAPIError as exc:
        _LOG.error(
            "payment_success_status_check_failed",
            extra={"track_id": charge_id, "error": str(exc)},
        )
    except Exception as exc:
        _LOG.error(
            "payment_success_unexpected_error",
            extra={"track_id": charge_id, "error": str(exc)},
        )
    return _render_payment_page(
        status="unknown",
        record=_payment_link_for(track_id, session_id, status_resp),
        check_url=check_url,
        recheck_url=recheck_url,
    )


# ── Branded /pay link ─────────────────────────────────────────────────────────

@app.get("/pay/{link_id}")
async def redirect_to_payment(link_id: str) -> Response:
    """Branded payment link: forwards to the UPayments checkout for that link."""
    from db import payment_links as db

    db.init_table()
    record = db.get_by_id(link_id)
    if record is None:
        page = _render_payment_page(status="inactive")
        page.status_code = 404
        return page
    charge_id = record.get("tap_charge_id") or ""
    if record.get("status") == "PAYMENT_CAPTURED":
        return _render_payment_page(
            status="paid", record=record, reference=_payment_reference({}, charge_id)
        )

    attempt, status_resp = _latest_attempt(charge_id)
    if attempt == "paid":
        # Captured at the gateway, webhook not processed yet. Never regenerate
        # here: a fresh charge would let the customer pay twice.
        return _render_payment_page(
            status="paid", record=record, reference=_payment_reference(status_resp, charge_id)
        )
    if attempt == "failed":
        fresh = _replace_failed_charge(record)
        if fresh is None:
            # Never send the customer back to the dead charge on the gateway.
            return _render_payment_page(status="retry_error", record=record)
        record = fresh
    return RedirectResponse(record["payment_url"], status_code=302)


def _latest_attempt(charge_id: str) -> tuple[Literal["paid", "failed", "open"], dict]:
    """Outcome of the stored charge's latest attempt; "open" when unknown or unattempted."""
    if not charge_id:
        return "open", {}
    try:
        from upayments.client import upayments_client_from_env

        status_resp = upayments_client_from_env().get_charge_status(charge_id)
        result = _payment_result_from_status_payload(status_resp).upper()
    except Exception:
        return "open", {}
    if result in _PAID_REDIRECT_RESULTS:
        return "paid", status_resp
    if result in _FAILED_REDIRECT_RESULTS or "NOT CAPTURED" in result:
        return "failed", status_resp
    return "open", status_resp


def _replace_failed_charge(record: dict) -> dict | None:
    """Swap a failed/cancelled charge for a fresh one, keeping the same /pay link."""
    from db import payment_links as db
    from upayments.client import upayments_client_from_env

    invoice_number = str(record.get("invoice_number") or record.get("invoice_id") or "")
    try:
        charge = upayments_client_from_env().create_charge(
            amount=float(record["amount"]),
            currency=str(record.get("currency") or "KWD"),
            customer_name=str(record.get("customer_name") or ""),
            customer_phone=str(record.get("whatsapp_to_number") or ""),
            invoice_number=invoice_number,
            description=f"Invoice #{invoice_number} — {record.get('customer_name') or ''}".strip(" —"),
        )
    except Exception as exc:
        _LOG.error("pay_retry_charge_failed", extra={"link_id": record.get("id"), "error": str(exc)})
        return None
    payment_url = str(charge.get("payment_url") or "")
    charge_id = str(charge.get("charge_id") or "")
    if not payment_url or not charge_id:
        return None
    swapped = db.replace_charge(
        record["id"],
        tap_charge_id=charge_id,
        payment_url=payment_url,
        expected_charge_id=str(record.get("tap_charge_id") or ""),
    )
    if swapped:
        _LOG.info("pay_retry_charge_created", extra={"link_id": record.get("id"), "charge_id": charge_id})
    else:
        # A simultaneous retry already replaced the charge; use that one so
        # both taps land on the same checkout. Ours is left unused.
        _LOG.info("pay_retry_charge_superseded", extra={"link_id": record.get("id"), "charge_id": charge_id})
    return db.get_by_id(record["id"])


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
    webhook_db_path = os.getenv("WEBHOOK_DB_PATH", "webhook_events.db")

    # Which token file the app actually resolved, and a fingerprint of the
    # refresh token inside it. The fingerprint is a hash, never the token
    # itself — /health is unauthenticated. Comparing it against the value you
    # pasted into the dashboard tells you whether the running process picked
    # up the token you think it did.
    qbo_token_path = os.getenv("QBO_TOKEN_PATH", "tokens.json")
    qbo_token_fingerprint = None
    try:
        from auth.token_store import load_tokens

        _tokens = load_tokens(qbo_token_path)
        if _tokens is not None:
            qbo_token_fingerprint = hashlib.sha256(
                _tokens.refresh_token.encode("utf-8")
            ).hexdigest()[:12]
    except Exception:
        pass

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
        "webhook_db_path": webhook_db_path,
        "webhook_db_exists": os.path.exists(webhook_db_path),
        "qbo_webhook_delivery": dict(_QBO_WEBHOOK_STATS),
        "qbo_token_path": qbo_token_path,
        "qbo_token_file_exists": os.path.exists(qbo_token_path),
        "qbo_refresh_token_fingerprint": qbo_token_fingerprint,
        "qbo_force_bootstrap_enabled": force_bootstrap_requested(),
    }
