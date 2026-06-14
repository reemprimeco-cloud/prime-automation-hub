"""Admin WhatsApp inbound button actions (PAID / RESEND / STATUS)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from db import payment_links as db
from logging_config import get_logger
from qbo.client import QBOError, QuickBooksClient
from workflows.invoice_to_tap import _phone_for_tap

_LOG = get_logger("workflow.admin_whatsapp")

BUTTON_RE = re.compile(r"^(PAID|RESEND|STATUS)_(.+)$")


@dataclass
class AdminActionResult:
    reply: str
    ok: bool = True


def normalize_whatsapp_number(number: str) -> str:
    """Normalize Twilio WhatsApp address to E.164 (+965...)."""
    cleaned = (number or "").replace("whatsapp:", "").strip()
    if cleaned and not cleaned.startswith("+"):
        cleaned = f"+{cleaned.lstrip('+')}"
    return cleaned


def parse_button_payload(body: str, button_payload: str = "") -> tuple[str, str] | None:
    """Parse PAID_XXXX / RESEND_XXXX / STATUS_XXXX from inbound message."""
    for candidate in (button_payload, body):
        text = (candidate or "").strip()
        if not text:
            continue
        match = BUTTON_RE.match(text)
        if match:
            return match.group(1), match.group(2)
    return None


def _hub_invoice_pdf_url(invoice_id: str) -> str:
    return f"https://prime-automation-hub.onrender.com/invoice/{invoice_id}/pdf"


def _payment_url_for_invoice(invoice: dict, record: dict | None) -> str:
    if record and record.get("payment_url"):
        return record["payment_url"]
    note = invoice.get("PrivateNote") or ""
    match = re.search(r"Payment Link:\n(https://[^\n]+)", note)
    return match.group(1) if match else ""


def resend_customer_whatsapp(
    invoice: dict,
    *,
    qbo_client: QuickBooksClient,
    whatsapp_client,
    settings,
) -> bool:
    """Resend the customer payment/bank-transfer WhatsApp for an invoice."""
    invoice_id = str(invoice.get("Id", ""))
    invoice_number = str(invoice.get("DocNumber", invoice_id))
    amount = float(invoice.get("TotalAmt", 0))
    balance = float(invoice.get("Balance", 0))
    if balance <= 0:
        return False

    cref = invoice.get("CustomerRef") or {}
    customer = qbo_client.get_customer_by_id(str(cref.get("value", "")))
    phone = _phone_for_tap(customer)
    if not phone.number:
        return False

    wa_number = f"+{phone.country_code}{phone.number}"
    invoice_link = _hub_invoice_pdf_url(invoice_id)
    is_bank = "BANK_TRANSFER" in (customer.notes or "").upper()

    if is_bank:
        bank_info = (settings.bank_transfer_info or "").strip()
        if not bank_info:
            return False
        msg = whatsapp_client.send_payment_link(
            wa_number,
            customer_name=customer.display_name,
            invoice_number=invoice_number,
            amount=amount,
            payment_url=bank_info,
            invoice_link=invoice_link,
        )
    else:
        record = db.get_by_invoice_id(invoice_id)
        payment_url = _payment_url_for_invoice(invoice, record)
        if not payment_url:
            return False
        msg = whatsapp_client.send_payment_link(
            wa_number,
            customer_name=customer.display_name,
            invoice_number=invoice_number,
            amount=amount,
            payment_url=payment_url,
            invoice_link=invoice_link,
        )

    if msg.sent:
        db.mark_whatsapp_sent(invoice_id, message_sid=msg.sid, to_number=wa_number)
    return msg.sent


def handle_admin_action(
    action: str,
    doc_number: str,
    *,
    qbo_client: QuickBooksClient,
    whatsapp_client,
    settings,
) -> AdminActionResult:
    """Execute an admin button action and return a reply for the admin."""
    invoice = qbo_client.find_invoice_by_doc_number(doc_number)
    if invoice is None:
        return AdminActionResult(
            reply=f"Invoice #{doc_number} not found in QuickBooks.",
            ok=False,
        )

    invoice_id = str(invoice.get("Id", ""))
    customer_ref = invoice.get("CustomerRef") or {}
    customer_id = str(customer_ref.get("value", ""))
    customer_name = str(customer_ref.get("name", ""))
    amount = float(invoice.get("TotalAmt", 0))
    balance = float(invoice.get("Balance", 0))

    if action == "STATUS":
        if balance <= 0:
            status = "PAID"
        elif balance < amount:
            status = f"PARTIAL (balance {balance:.3f} KWD)"
        else:
            status = f"OPEN (balance {balance:.3f} KWD)"
        return AdminActionResult(
            reply=f"Invoice #{doc_number} — {customer_name}: {status}"
        )

    if action == "RESEND":
        if balance <= 0:
            return AdminActionResult(
                reply=f"Invoice #{doc_number} is already paid — not resent.",
                ok=False,
            )
        if whatsapp_client is None:
            return AdminActionResult(reply="WhatsApp is not configured.", ok=False)
        sent = resend_customer_whatsapp(
            invoice,
            qbo_client=qbo_client,
            whatsapp_client=whatsapp_client,
            settings=settings,
        )
        if sent:
            return AdminActionResult(
                reply=f"Invoice #{doc_number} resent to {customer_name}."
            )
        return AdminActionResult(
            reply=f"Could not resend invoice #{doc_number} (no valid mobile or link).",
            ok=False,
        )

    if action == "PAID":
        if balance <= 0:
            return AdminActionResult(
                reply=f"Invoice #{doc_number} is already paid in QuickBooks."
            )
        try:
            payment = qbo_client.create_bank_transfer_payment(
                customer_id=customer_id,
                invoice_id=invoice_id,
                amount=balance,
            )
            payment_id = str(payment.get("Id", ""))
            record = db.get_by_invoice_id(invoice_id)
            if record and record.get("status") != "PAYMENT_CAPTURED":
                db.mark_payment_captured(invoice_id, qbo_payment_id=payment_id)
            _LOG.info(
                "admin_bank_payment_created",
                extra={
                    "invoice_id": invoice_id,
                    "doc_number": doc_number,
                    "qbo_payment_id": payment_id,
                },
            )
            return AdminActionResult(
                reply=(
                    f"Invoice #{doc_number} marked PAID in QBO "
                    f"(payment {payment_id}, {balance:.3f} KWD)."
                )
            )
        except QBOError as exc:
            _LOG.error(
                "admin_bank_payment_failed",
                extra={"invoice_id": invoice_id, "error": str(exc)},
            )
            return AdminActionResult(
                reply=f"Failed to mark invoice #{doc_number} paid: {exc}",
                ok=False,
            )

    return AdminActionResult(reply=f"Unknown action: {action}", ok=False)
