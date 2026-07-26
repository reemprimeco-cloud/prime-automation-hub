"""Invoice → UPayments payment link workflow.

Orchestrates:
  1. Idempotency check — return existing link if still valid
  2. Fetch and validate the QBO invoice (skip paid / voided)
  3. Fetch customer details
  4. Normalise Kuwait mobile for UPayments
  5. Create UPayments hosted payment link
  6. Persist to database (``tap_charge_id`` column stores provider charge id)
  7. Write the link back to the QBO invoice's PrivateNote
  8. Send WhatsApp payment link (best-effort)

Legacy Tap ``inv_`` / ``chg_`` rows are treated as stale and regenerated on
UPayments. The ``/webhook/tap`` route remains for any in-flight Tap payments.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from db import payment_links as db
from logging_config import get_logger
from qbo.client import QBOError, QuickBooksClient
from qbo.phone import is_kuwait_mobile, normalize
from tap.models import TapPhoneNumber
from upayments.client import UPaymentsClient
from upayments.exceptions import UPaymentsAPIError

_LOG = get_logger("workflow.invoice_to_tap")

_STALE_UPAYMENT_RESULTS = frozenset({
    "NOT CAPTURED",
    "FAILED",
    "CANCELED",
    "CANCELLED",
    "EXPIRED",
    "VOIDED",
    "DECLINED",
    "ERROR",
})
_PAID_UPAYMENT_RESULTS = frozenset({"CAPTURED", "PAID", "SUCCESS"})


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class LinkResult:
    """Returned by process_invoice() on success or idempotency hit."""
    invoice_id: str
    invoice_number: str
    customer_name: str
    amount: float
    currency: str
    tap_charge_id: str  # provider charge id (UPayments track/session id)
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
    reason: str               # "ALREADY_PAID" | "VOIDED" | "BANK_TRANSFER" | "MISSING_PHONE"
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
    """Return the best Kuwait mobile number, or empty if none valid.

    Name retained for call-site compatibility; used for UPayments + WhatsApp.
    """
    for raw in [customer.mobile, customer.phone, customer.alternate_phone]:
        norm = normalize(raw)
        if norm and is_kuwait_mobile(norm):      # mobiles only — landlines excluded
            return TapPhoneNumber(country_code="965", number=norm[4:])
    return TapPhoneNumber(country_code="", number="")


def _split_name(display_name: str) -> tuple[str, str]:
    """Split 'Ahmed Al-Rashid' → ('Ahmed', 'Al-Rashid'). Handles single names."""
    parts = display_name.strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else ".")


def _e164(phone: TapPhoneNumber) -> str:
    if not phone.number:
        return ""
    return f"+{phone.country_code}{phone.number}"


# ── QBO note format ───────────────────────────────────────────────────────────

def _build_private_note(
    existing_note: str,
    payment_url: str,
    charge_id: str,
    generated_at: str,
) -> str:
    parts = []
    if existing_note and existing_note.strip():
        parts.append(existing_note.strip())
    parts.append(
        f"--- Prime Automation Hub ---\n"
        f"Payment Link:\n{payment_url}\n\n"
        f"UPayments Reference:\n{charge_id}\n\n"
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


def _is_legacy_tap_reference(charge_id: str) -> bool:
    cid = (charge_id or "").strip()
    return cid.startswith("inv_") or cid.startswith("chg_")


def _is_stale_upayments_link(upayments_client: UPaymentsClient, charge_id: str) -> bool:
    """True when the stored link should be regenerated."""
    if not charge_id:
        return True
    # Old Tap references → cut over to UPayments on next process
    if _is_legacy_tap_reference(charge_id):
        return True
    # Full payment URL stored as id (pre-session_id fix) — regenerate cleanly
    if charge_id.startswith("http"):
        return True

    try:
        data = upayments_client.get_charge_status(charge_id)
    except UPaymentsAPIError as exc:
        _LOG.warning(
            "upayments_stale_check_failed",
            extra={"charge_id": charge_id, "error": str(exc)},
        )
        return False

    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    result = str(
        nested.get("result") or nested.get("payment_status") or nested.get("status") or ""
    ).strip().upper()
    if result in _PAID_UPAYMENT_RESULTS or result in _STALE_UPAYMENT_RESULTS:
        return True
    return False


# ── main entry point ──────────────────────────────────────────────────────────

def process_invoice(
    invoice_id: str,
    *,
    qbo_client: QuickBooksClient,
    upayments_client: UPaymentsClient,
    settings,
    whatsapp_client=None,          # messaging.whatsapp.WhatsAppClient | None
    tap_client=None,               # ignored — kept for call-site compat during cutover
) -> LinkResult | SkipResult:
    """Run the full workflow for a single QBO invoice.

    Returns:
      LinkResult   — a new link was generated, or an existing one was returned
      SkipResult   — the invoice is paid/void and should not get a link
    Raises:
      QBOError           — QuickBooks API failure
      UPaymentsAPIError  — UPayments API failure
    """
    _LOG.info("workflow_start", extra={"invoice_id": invoice_id, "provider": "upayments"})

    # ── 1. Fetch + validate invoice ───────────────────────────────────────────
    invoice = qbo_client.get_invoice(invoice_id)
    skip = _validate_invoice(invoice)
    if skip:
        return skip

    amount_due = float(invoice.get("Balance", 0))
    total_amt = float(invoice.get("TotalAmt", 0))

    # ── 2. Idempotency check (regenerate when balance due changed) ────────────
    db.init_table()
    existing = db.get_by_invoice_id(invoice_id)
    if existing:
        charge_id = existing["tap_charge_id"]
        stored_amount = float(existing.get("amount", 0))
        try:
            stale = _is_stale_upayments_link(upayments_client, charge_id)
            balance_changed = abs(amount_due - stored_amount) >= 0.001
            partially_paid = amount_due < total_amt - 0.001
            link_covers_more_than_due = stored_amount > amount_due + 0.001
            needs_regen = (
                stale
                or balance_changed
                or (partially_paid and link_covers_more_than_due)
            )
            if needs_regen:
                reason = (
                    "stale_link_regenerating"
                    if stale
                    else "balance_changed_regenerating"
                    if balance_changed
                    else "partial_payment_regenerating"
                )
                _LOG.info(
                    reason,
                    extra={
                        "invoice_id": invoice_id,
                        "charge_id": charge_id,
                        "stored_amount": stored_amount,
                        "amount_due": amount_due,
                        "total_amt": total_amt,
                        "stale": stale,
                        "balance_changed": balance_changed,
                        "partially_paid": partially_paid,
                    },
                )
                db.delete_by_invoice_id(invoice_id)
            else:
                _LOG.info(
                    "idempotency_hit",
                    extra={"invoice_id": invoice_id, "charge_id": charge_id},
                )
                return _existing_link_result(existing)
        except UPaymentsAPIError as exc:
            _LOG.warning(
                "stale_charge_check_failed",
                extra={
                    "invoice_id": invoice_id,
                    "charge_id": charge_id,
                    "error": str(exc),
                },
            )
            return _existing_link_result(existing)

    invoice_number  = str(invoice.get("DocNumber", invoice_id))
    currency        = "KWD"          # hardcoded for this Kuwait deployment
    existing_note   = (invoice.get("PrivateNote") or "").strip()
    sync_token      = str(invoice.get("SyncToken", "0"))
    customer_ref    = invoice.get("CustomerRef") or {}
    qbo_customer_id = str(customer_ref.get("value", ""))
    customer_display_name = str(customer_ref.get("name", ""))
    invoice_link = f"https://prime-automation-hub.onrender.com/invoice/{invoice_id}/pdf"

    # ── 3. Fetch customer ─────────────────────────────────────────────────────
    customer = qbo_client.get_customer_by_id(qbo_customer_id)

    if "BANK_TRANSFER" in (customer.notes or "").upper():
        _LOG.info("workflow_skipped_bank_transfer", extra={"invoice_id": invoice_id})
        bank_info = (settings.bank_transfer_info or "").strip()
        if whatsapp_client is not None and bank_info:
            phone = _phone_for_tap(customer)
            if phone.number:
                wa_number = _e164(phone)
                msg = whatsapp_client.send_payment_link(
                    wa_number,
                    customer_name=customer.display_name,
                    invoice_number=invoice_number,
                    amount=amount_due,
                    currency=currency,
                    payment_url=bank_info,
                    invoice_link=invoice_link,
                )
                if msg.sent:
                    _LOG.info(
                        "bank_transfer_whatsapp_sent",
                        extra={"invoice_id": invoice_id, "to": wa_number},
                    )
                    whatsapp_client.send_admin_bank_notify(
                        invoice_number, customer.display_name, amount_due, currency
                    )
                else:
                    _LOG.warning(
                        "bank_transfer_whatsapp_failed",
                        extra={"invoice_id": invoice_id, "error": msg.error},
                    )
        return SkipResult(
            invoice_id=invoice_id,
            reason="BANK_TRANSFER",
            detail="Customer prefers bank transfer — no UPayments link generated.",
        )

    phone = _phone_for_tap(customer)
    if not phone.number:
        _LOG.error(
            "missing_customer_phone",
            extra={
                "invoice_id": invoice_id,
                "invoice_number": invoice_number,
                "customer_id": qbo_customer_id,
                "customer_name": customer.display_name,
            },
        )
        if whatsapp_client is not None:
            whatsapp_client.send_admin_missing_phone_alert(
                invoice_number, customer.display_name
            )
        return SkipResult(
            invoice_id=invoice_id,
            reason="MISSING_PHONE",
            detail=(
                f"Customer '{customer.display_name}' has no valid Kuwait mobile "
                "number in QBO — payment link not generated."
            ),
        )

    customer_phone = _e164(phone)
    customer_name = customer.display_name or customer_display_name or "Customer"

    # ── 4–5. Create UPayments hosted charge ───────────────────────────────────
    # requested_order_id / order.id = DocNumber so webhook matches payment_links
    charge = upayments_client.create_charge(
        amount=amount_due,
        currency=currency,
        customer_name=customer_name,
        customer_phone=customer_phone,
        invoice_number=invoice_number,
        description=f"Invoice #{invoice_number} — {customer_name}",
        customer_email=customer.email or "",
        customer_unique_id=qbo_customer_id or invoice_number,
    )
    payment_url = str(charge.get("payment_url") or "")
    charge_id = str(charge.get("charge_id") or "")
    if not payment_url or not charge_id:
        raise UPaymentsAPIError(
            f"UPayments create_charge missing payment_url/charge_id: {charge!r}"
        )

    generated_at = datetime.now(timezone.utc).isoformat()

    # ── 6. Persist to database ────────────────────────────────────────────────
    db.create_link(
        invoice_id=invoice_id,
        customer_id=qbo_customer_id,
        tap_charge_id=charge_id,
        payment_url=payment_url,
        amount=amount_due,
        currency=currency,
        invoice_number=invoice_number,
        customer_name=customer.display_name,
    )

    # ── 7. Write back to QBO (best-effort — DB record is the source of truth) ─
    qbo_updated = False
    try:
        note = _build_private_note(
            existing_note, payment_url, charge_id, generated_at
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
        wa_number = customer_phone
        msg = whatsapp_client.send_payment_link(
            wa_number,
            customer_name=customer.display_name,
            invoice_number=invoice_number,
            amount=amount_due,
            currency=currency,
            payment_url=payment_url,
            invoice_link=invoice_link,
        )
        if msg.sent:
            db.mark_whatsapp_sent(
                invoice_id, message_sid=msg.sid, to_number=wa_number
            )
            wa_sent = True
            wa_sid = msg.sid
            whatsapp_client.send_admin_tap_notify(
                invoice_number, customer.display_name, amount_due, currency
            )
        else:
            db.mark_whatsapp_failed(invoice_id, error=msg.error)
    else:
        _LOG.debug("whatsapp_skipped_no_client", extra={"invoice_id": invoice_id})

    _LOG.info(
        "workflow_complete",
        extra={
            "invoice_id": invoice_id,
            "charge_id": charge_id,
            "provider": "upayments",
            "qbo_updated": qbo_updated,
            "whatsapp_sent": wa_sent,
            "amount_due": amount_due,
            "total_amt": total_amt,
        },
    )

    return LinkResult(
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        customer_name=customer.display_name,
        amount=amount_due,
        currency=currency,
        tap_charge_id=charge_id,
        payment_url=payment_url,
        status="LINK_GENERATED",
        qbo_note_updated=qbo_updated,
        created_at=generated_at,
        whatsapp_sent=wa_sent,
        whatsapp_number=wa_number,
        whatsapp_message_sid=wa_sid,
    )
