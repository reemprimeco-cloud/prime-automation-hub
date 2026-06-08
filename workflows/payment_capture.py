"""Payment capture workflow.

Triggered by Tap's charge.captured webhook. Performs three actions:
  1. Creates a QBO payment — invoice status goes to Paid ✓
  2. Updates the payment_links record — status → PAYMENT_CAPTURED
  3. Sends WhatsApp confirmation to the customer (best-effort)

Idempotency: if the same charge_id is received twice, the second call returns
the existing result without re-creating the QBO payment.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from db import payment_links as db
from logging_config import get_logger
from qbo.client import QBOError, QuickBooksClient

_LOG = get_logger("workflow.payment_capture")


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class CaptureResult:
    tap_charge_id: str
    invoice_id: str
    invoice_number: str
    customer_name: str
    amount: float
    currency: str
    qbo_payment_id: str
    qbo_payment_created: bool
    whatsapp_sent: bool
    whatsapp_number: str
    status: str   # "PAYMENT_CAPTURED" | "ALREADY_CAPTURED"


@dataclass
class CaptureError:
    tap_charge_id: str
    reason: str   # "CHARGE_NOT_FOUND" | "QBO_ERROR"
    detail: str = ""


# ── main entry point ──────────────────────────────────────────────────────────

def handle_payment_capture(
    tap_charge_id: str,
    amount: float,
    currency: str = "KWD",
    *,
    qbo_client: QuickBooksClient,
    whatsapp_client=None,
) -> CaptureResult | CaptureError:
    """Process a captured Tap payment end-to-end.

    Returns CaptureResult on success or idempotency hit.
    Returns CaptureError when the charge is unknown or QBO fails.
    Never raises — all errors are returned as CaptureError.
    """
    _LOG.info("capture_start", extra={"tap_charge_id": tap_charge_id})

    # ── 1. Look up the payment link record ────────────────────────────────────
    record = db.get_by_charge_id(tap_charge_id)
    if record is None:
        _LOG.warning("capture_charge_not_found", extra={"tap_charge_id": tap_charge_id})
        return CaptureError(
            tap_charge_id=tap_charge_id,
            reason="CHARGE_NOT_FOUND",
            detail="No payment_link record found for this charge ID.",
        )

    invoice_id     = record["invoice_id"]
    customer_id    = record["customer_id"]
    invoice_number = record.get("invoice_number") or ""
    customer_name  = record.get("customer_name") or ""
    link_amount    = float(record.get("amount") or amount)

    if not invoice_number or not customer_name:
        try:
            invoice = qbo_client.get_invoice(invoice_id)
            if not invoice_number:
                invoice_number = str(invoice.get("DocNumber") or invoice_id)
            if not customer_name:
                cref = invoice.get("CustomerRef") or {}
                customer_name = str(cref.get("name") or "")
        except QBOError as exc:
            _LOG.warning(
                "capture_invoice_lookup_failed",
                extra={"invoice_id": invoice_id, "error": str(exc)},
            )

    # ── 2. Idempotency — already captured ─────────────────────────────────────
    if record.get("status") == "PAYMENT_CAPTURED":
        _LOG.info("capture_already_done", extra={"invoice_id": invoice_id})
        return CaptureResult(
            tap_charge_id=tap_charge_id,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            customer_name=customer_name,
            amount=link_amount,
            currency=record.get("currency", currency),
            qbo_payment_id=record.get("qbo_payment_id") or "",
            qbo_payment_created=True,
            whatsapp_sent=bool(record.get("whatsapp_sent")),
            whatsapp_number=record.get("whatsapp_to_number") or "",
            status="ALREADY_CAPTURED",
        )

    # ── 3. Create QBO payment (marks invoice as Paid) ─────────────────────────
    qbo_payment_id = ""
    try:
        payment = qbo_client.create_payment(
            customer_id=customer_id,
            invoice_id=invoice_id,
            amount=link_amount,
            tap_charge_id=tap_charge_id,
        )
        qbo_payment_id = str(payment.get("Id", ""))
        _LOG.info(
            "qbo_payment_created",
            extra={"invoice_id": invoice_id, "qbo_payment_id": qbo_payment_id},
        )
    except QBOError as exc:
        _LOG.error(
            "qbo_payment_failed",
            extra={"invoice_id": invoice_id, "error": str(exc)},
        )
        return CaptureError(
            tap_charge_id=tap_charge_id,
            reason="QBO_ERROR",
            detail=str(exc),
        )

    # ── 4. Update DB ──────────────────────────────────────────────────────────
    db.mark_payment_captured(invoice_id, qbo_payment_id=qbo_payment_id)

    # ── 5. WhatsApp confirmation (best-effort) ────────────────────────────────
    wa_sent   = False
    wa_number = record.get("whatsapp_to_number") or ""

    if whatsapp_client is not None and not wa_number:
        try:
            customer = qbo_client.get_customer_by_id(customer_id)
            from workflows.invoice_to_tap import _phone_for_tap

            phone = _phone_for_tap(customer)
            if phone.number:
                wa_number = f"+{phone.country_code}{phone.number}"
                if not customer_name:
                    customer_name = customer.display_name
        except QBOError as exc:
            _LOG.warning(
                "capture_customer_lookup_failed",
                extra={"customer_id": customer_id, "error": str(exc)},
            )

    if whatsapp_client is not None and wa_number:
        msg = whatsapp_client.send_payment_confirmation(
            wa_number,
            customer_name=customer_name or "Customer",
            invoice_number=invoice_number,
            amount=link_amount,
            currency=currency,
        )
        if msg.sent:
            db.mark_whatsapp_sent(invoice_id, message_sid=msg.sid, to_number=wa_number)
            wa_sent = True
            _LOG.info("confirmation_whatsapp_sent", extra={"to": wa_number})
        else:
            _LOG.warning("confirmation_whatsapp_failed", extra={"error": msg.error})
    elif whatsapp_client is not None and not wa_number:
        _LOG.warning("confirmation_no_number", extra={"invoice_id": invoice_id})

    _LOG.info(
        "capture_complete",
        extra={
            "invoice_id": invoice_id,
            "qbo_payment_id": qbo_payment_id,
            "whatsapp_sent": wa_sent,
        },
    )

    return CaptureResult(
        tap_charge_id=tap_charge_id,
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        customer_name=customer_name,
        amount=link_amount,
        currency=currency,
        qbo_payment_id=qbo_payment_id,
        qbo_payment_created=True,
        whatsapp_sent=wa_sent,
        whatsapp_number=wa_number,
        status="PAYMENT_CAPTURED",
    )
