"""Invoice-to-Tap payment link workflow.

Orchestrates the complete Phase 3 flow:
  1. Idempotency check — return existing link if already generated
  2. Fetch and validate the QBO invoice (skip paid / voided)
  3. Fetch customer details
  4. Normalise phone number for Tap
  5. Create Tap hosted payment link
  6. Persist to database
  7. Write the link back to the QBO invoice's PrivateNote

The caller owns the QBO and Tap clients — the workflow itself is stateless.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from db import payment_links as db
from logging_config import get_logger
from qbo.client import QBOError, QuickBooksClient
from qbo.phone import is_kuwait_mobile, normalize
from tap.client import TapClient
from tap.exceptions import TapError
from tap.models import CreateChargeRequest, TapCustomer, TapPhoneNumber

_LOG = get_logger("workflow.invoice_to_tap")

_STALE_CHARGE_STATUSES = frozenset({"CANCELLED", "ABANDONED", "EXPIRED"})


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class LinkResult:
    """Returned by process_invoice() on success or idempotency hit."""
    invoice_id: str
    invoice_number: str
    customer_name: str
    amount: float
    currency: str
    tap_charge_id: str
    payment_url: str
    status: str               # "LINK_GENERATED" or "EXISTING_LINK_RETURNED"
    qbo_note_updated: bool
    created_at: str
    whatsapp_sent: bool = False
    whatsapp_number: str = ""
    whatsapp_message_sid: str = ""


@dataclass
class SkipResult:
    """Returned when the invoice should not get a payment link."""
    invoice_id: str
    reason: str               # "ALREADY_PAID" | "VOIDED" | "BANK_TRANSFER"
    detail: str = ""


# ── invoice validation ────────────────────────────────────────────────────────

def _validate_invoice(invoice: dict) -> SkipResult | None:
    """Return a SkipResult if the invoice should be skipped, or None if it's fine."""
    invoice_id = str(invoice.get("Id", ""))
    total_amt = float(invoice.get("TotalAmt", 0))
    balance   = float(invoice.get("Balance", 0))

    if total_amt == 0:
        _LOG.info("invoice_skip_voided", extra={"invoice_id": invoice_id})
        return SkipResult(invoice_id=invoice_id, reason="VOIDED")

    if balance <= 0:
        _LOG.info("invoice_skip_paid", extra={"invoice_id": invoice_id, "balance": balance})
        return SkipResult(invoice_id=invoice_id, reason="ALREADY_PAID")

    return None


# ── phone helpers ─────────────────────────────────────────────────────────────

def _phone_for_tap(customer) -> TapPhoneNumber:
    """Return the best Kuwait mobile number split for Tap, or empty if none valid."""
    for raw in [customer.mobile, customer.phone, customer.alternate_phone]:
        norm = normalize(raw)
        if norm and is_kuwait_mobile(norm):      # mobiles only — landlines excluded
            return TapPhoneNumber(country_code="965", number=norm[4:])
    return TapPhoneNumber(country_code="", number="")


def _split_name(display_name: str) -> tuple[str, str]:
    """Split 'Ahmed Al-Rashid' → ('Ahmed', 'Al-Rashid'). Handles single names."""
    parts = display_name.strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else ".")


# ── QBO note format ───────────────────────────────────────────────────────────

def _build_private_note(
    existing_note: str,
    payment_url: str,
    tap_charge_id: str,
    generated_at: str,
) -> str:
    parts = []
    if existing_note and existing_note.strip():
        parts.append(existing_note.strip())
    parts.append(
        f"--- Prime Automation Hub ---\n"
        f"Payment Link:\n{payment_url}\n\n"
        f"Tap Reference:\n{tap_charge_id}\n\n"
        f"Generated At:\n{generated_at}"
    )
    return "\n\n".join(parts)


def _existing_link_result(existing: dict) -> LinkResult:
    """Build a LinkResult from a stored payment_links row."""
    return LinkResult(
        invoice_id=existing["invoice_id"],
        invoice_number=existing.get("invoice_number", ""),
        customer_name=existing.get("customer_name", ""),
        amount=existing["amount"],
        currency=existing["currency"],
        tap_charge_id=existing["tap_charge_id"],
        payment_url=existing["payment_url"],
        status="EXISTING_LINK_RETURNED",
        qbo_note_updated=bool(existing["qbo_note_updated"]),
        created_at=existing["created_at"],
    )


def _is_stale_tap_charge(tap_client: TapClient, tap_charge_id: str) -> bool:
    """True when the Tap charge can no longer be paid and should be regenerated."""
    charge = tap_client.get_charge(tap_charge_id)
    status = str(charge.get("status", "")).upper()
    return status in _STALE_CHARGE_STATUSES


# ── main entry point ──────────────────────────────────────────────────────────

def process_invoice(
    invoice_id: str,
    *,
    qbo_client: QuickBooksClient,
    tap_client: TapClient,
    settings,
    whatsapp_client=None,          # messaging.whatsapp.WhatsAppClient | None
) -> LinkResult | SkipResult:
    """Run the full workflow for a single QBO invoice.

    Returns:
      LinkResult   — a new link was generated, or an existing one was returned
      SkipResult   — the invoice is paid/void and should not get a link
    Raises:
      QBOError     — QuickBooks API failure
      TapError     — Tap Payments API failure
    """
    _LOG.info("workflow_start", extra={"invoice_id": invoice_id})

    # ── 1. Idempotency check ──────────────────────────────────────────────────
    db.init_table()
    existing = db.get_by_invoice_id(invoice_id)
    if existing:
        tap_charge_id = existing["tap_charge_id"]
        try:
            if _is_stale_tap_charge(tap_client, tap_charge_id):
                _LOG.info(
                    "stale_charge_regenerating",
                    extra={"invoice_id": invoice_id, "tap_charge_id": tap_charge_id},
                )
                db.delete_by_invoice_id(invoice_id)
            else:
                _LOG.info(
                    "idempotency_hit",
                    extra={"invoice_id": invoice_id, "tap_charge_id": tap_charge_id},
                )
                return _existing_link_result(existing)
        except TapError as exc:
            _LOG.warning(
                "stale_charge_check_failed",
                extra={
                    "invoice_id": invoice_id,
                    "tap_charge_id": tap_charge_id,
                    "error": str(exc),
                },
            )
            return _existing_link_result(existing)

    # ── 2. Fetch + validate invoice ───────────────────────────────────────────
    invoice = qbo_client.get_invoice(invoice_id)
    skip = _validate_invoice(invoice)
    if skip:
        return skip

    invoice_number  = str(invoice.get("DocNumber", invoice_id))
    total_amt       = float(invoice.get("TotalAmt", 0))
    currency        = "KWD"          # hardcoded for this Kuwait deployment
    existing_note   = (invoice.get("PrivateNote") or "").strip()
    sync_token      = str(invoice.get("SyncToken", "0"))
    customer_ref    = invoice.get("CustomerRef") or {}
    qbo_customer_id = str(customer_ref.get("value", ""))
    customer_display_name = str(customer_ref.get("name", ""))

    # ── 3. Fetch customer ─────────────────────────────────────────────────────
    customer = qbo_client.get_customer_by_id(qbo_customer_id)

    if "BANK_TRANSFER" in (customer.notes or "").upper():
        _LOG.info("workflow_skipped_bank_transfer", extra={"invoice_id": invoice_id})
        return SkipResult(
            invoice_id=invoice_id,
            reason="BANK_TRANSFER",
            detail="Customer prefers bank transfer — no Tap link generated.",
        )

    # ── 4. Build Tap request ──────────────────────────────────────────────────
    first, last = _split_name(customer.display_name or customer_display_name)
    tap_customer = TapCustomer(
        first_name=first,
        last_name=last,
        email=customer.email or "",
        phone=_phone_for_tap(customer),
    )
    tap_request = CreateChargeRequest(
        amount=total_amt,
        currency=currency,
        description=f"Invoice #{invoice_number} — {customer.display_name}",
        customer=tap_customer,
        transaction_ref=f"INV-{invoice_number}",
        order_ref=invoice_id,
        redirect_url=settings.tap_redirect_url,
        webhook_url=settings.tap_webhook_url,
        metadata={
            "qbo_invoice_id": invoice_id,
            "invoice_number": invoice_number,
            "qbo_customer_id": qbo_customer_id,
        },
    )

    # ── 5. Create Tap payment link ────────────────────────────────────────────
    charge = tap_client.create_charge(tap_request)
    generated_at = datetime.now(timezone.utc).isoformat()

    # ── 6. Persist to database ────────────────────────────────────────────────
    db.create_link(
        invoice_id=invoice_id,
        customer_id=qbo_customer_id,
        tap_charge_id=charge.charge_id,
        payment_url=charge.payment_url,
        amount=total_amt,
        currency=currency,
        invoice_number=invoice_number,
        customer_name=customer.display_name,
    )

    # ── 7. Write back to QBO (best-effort — DB record is the source of truth) ─
    qbo_updated = False
    try:
        note = _build_private_note(
            existing_note, charge.payment_url, charge.charge_id, generated_at
        )
        qbo_client.update_invoice_note(invoice_id, sync_token, note)
        db.mark_qbo_updated(invoice_id)
        qbo_updated = True
        _LOG.info("qbo_note_updated", extra={"invoice_id": invoice_id})
    except QBOError as exc:
        _LOG.error(
            "qbo_note_update_failed",
            extra={"invoice_id": invoice_id, "error": str(exc)},
        )

    # ── 8. Send WhatsApp (best-effort — payment link works regardless) ────────
    wa_sent = False
    wa_number = ""
    wa_sid = ""

    if whatsapp_client is not None:
        phone = _phone_for_tap(customer)
        if phone.number:
            wa_number = f"+{phone.country_code}{phone.number}"
            msg = whatsapp_client.send_payment_link(
                wa_number,
                customer_name=customer.display_name,
                invoice_number=invoice_number,
                amount=total_amt,
                currency=currency,
                payment_url=charge.payment_url,
            )
            if msg.sent:
                db.mark_whatsapp_sent(
                    invoice_id, message_sid=msg.sid, to_number=wa_number
                )
                wa_sent = True
                wa_sid = msg.sid
            else:
                db.mark_whatsapp_failed(invoice_id, error=msg.error)
        else:
            _LOG.warning(
                "whatsapp_skipped_no_mobile",
                extra={"invoice_id": invoice_id, "customer_id": qbo_customer_id},
            )
    else:
        _LOG.debug("whatsapp_skipped_no_client", extra={"invoice_id": invoice_id})

    _LOG.info(
        "workflow_complete",
        extra={
            "invoice_id": invoice_id,
            "tap_charge_id": charge.charge_id,
            "qbo_updated": qbo_updated,
            "whatsapp_sent": wa_sent,
        },
    )

    return LinkResult(
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        customer_name=customer.display_name,
        amount=total_amt,
        currency=currency,
        tap_charge_id=charge.charge_id,
        payment_url=charge.payment_url,
        status="LINK_GENERATED",
        qbo_note_updated=qbo_updated,
        created_at=generated_at,
        whatsapp_sent=wa_sent,
        whatsapp_number=wa_number,
        whatsapp_message_sid=wa_sid,
    )
