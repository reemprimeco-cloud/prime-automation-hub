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
from html import escape
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, Response

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


def _render_payment_page(
    status: Literal["success", "failed", "unknown"],
) -> HTMLResponse:
    """Customer-facing HTML after UPayments redirect (verified server-side)."""
    if status == "success":
        if _PAYMENT_SUCCESS_PAGE.is_file():
            return HTMLResponse(
                _PAYMENT_SUCCESS_PAGE.read_text(encoding="utf-8"),
                media_type="text/html; charset=utf-8",
            )
        title = "Payment successful"
        heading = "Payment successful"
        body = (
            "Thank you for your payment. Your invoice will be updated shortly and you "
            "will receive a confirmation on WhatsApp."
        )
        icon = "✓"
        accent = "#1f7a4d"
        soft = "#e8f5ee"
        actions = (
            '<a class="button" href="https://www.primeprint.com.kw">Back to Prime Print</a>'
        )
    elif status == "failed":
        title = "Payment not completed — Prime Print"
        heading = "Payment not completed"
        body = (
            "Your payment was not completed. No charge was made, or the payment was cancelled. "
            "If you believe this is an error, please contact us on WhatsApp and we will help."
        )
        icon = "!"
        accent = "#9a3412"
        soft = "#ffedd5"
        actions = (
            f'<a class="button" href="{_SUPPORT_WHATSAPP_URL}">Message us on WhatsApp</a>'
            '<div style="margin-top:12px">'
            '<a href="https://www.primeprint.com.kw" style="color:#5f6f63">Back to Prime Print</a>'
            "</div>"
        )
    else:
        title = "Confirming payment — Prime Print"
        heading = "We're confirming your payment"
        body = (
            "We're confirming your payment status. You'll receive a WhatsApp confirmation "
            "once it is verified. You can safely close this page."
        )
        icon = "…"
        accent = "#334155"
        soft = "#e2e8f0"
        actions = (
            f'<a class="button" href="{_SUPPORT_WHATSAPP_URL}">Message us on WhatsApp</a>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escape(title)}</title>
    <style>
      :root {{ color-scheme: light; --bg:#f4f7f5; --card:#fff; --text:#1a2e1f;
        --muted:#5f6f63; --accent:{accent}; --accent-soft:{soft}; --border:#d8e3dc; }}
      * {{ box-sizing: border-box; }}
      body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
        font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        background:linear-gradient(180deg,var(--bg) 0%,#e9f0eb 100%); color:var(--text); }}
      .card {{ width:min(100%,440px); background:var(--card); border:1px solid var(--border);
        border-radius:16px; padding:32px 28px; box-shadow:0 12px 40px rgba(26,46,31,.08);
        text-align:center; }}
      .icon {{ width:72px; height:72px; margin:0 auto 20px; border-radius:50%; display:grid;
        place-items:center; background:var(--accent-soft); color:var(--accent);
        font-size:36px; line-height:1; }}
      h1 {{ margin:0 0 10px; font-size:1.5rem; font-weight:700; }}
      p {{ margin:0; color:var(--muted); line-height:1.6; }}
      .actions {{ margin-top:24px; }}
      a.button {{ display:inline-block; padding:12px 18px; border-radius:999px;
        background:var(--accent); color:#fff; text-decoration:none; font-weight:600; }}
    </style>
  </head>
  <body>
    <main class="card">
      <div class="icon" aria-hidden="true">{escape(icon)}</div>
      <h1>{escape(heading)}</h1>
      <p>{escape(body)}</p>
      <div class="actions">{actions}</div>
    </main>
  </body>
</html>"""
    return HTMLResponse(html, media_type="text/html; charset=utf-8")


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
        return _render_payment_page(status="unknown")

    from upayments.client import upayments_client_from_env
    from upayments.exceptions import UPaymentsAPIError

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
            return _render_payment_page(status="success")

        if (
            result_upper in _FAILED_REDIRECT_RESULTS
            or "NOT CAPTURED" in result_upper
        ):
            _LOG.info(
                "payment_success_route_hit_but_not_paid",
                extra={"track_id": charge_id, "result": result},
            )
            return _render_payment_page(status="failed")

        # PENDING / empty / AUTHORIZED / unknown → never claim success or failure
        _LOG.info(
            "payment_success_status_pending_or_unknown",
            extra={"track_id": charge_id, "result": result},
        )
        return _render_payment_page(status="unknown")
    except UPaymentsAPIError as exc:
        _LOG.error(
            "payment_success_status_check_failed",
            extra={"track_id": charge_id, "error": str(exc)},
        )
        return _render_payment_page(status="unknown")
    except Exception as exc:
        _LOG.error(
            "payment_success_unexpected_error",
            extra={"track_id": charge_id, "error": str(exc)},
        )
        return _render_payment_page(status="unknown")


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
        "qbo_token_path": qbo_token_path,
        "qbo_token_file_exists": os.path.exists(qbo_token_path),
        "qbo_refresh_token_fingerprint": qbo_token_fingerprint,
        "qbo_force_bootstrap_enabled": force_bootstrap_requested(),
    }
