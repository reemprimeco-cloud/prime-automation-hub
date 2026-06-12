"""Admin WhatsApp inbound action handler.

Parses button payloads from the admin's WhatsApp and dispatches:
  PAID_XXXX    — mark invoice paid via bank transfer in QBO
  RESEND_XXXX  — resend payment link WhatsApp to customer
  STATUS_XXXX  — reply with current invoice status
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from logging_config import get_logger

_LOG = get_logger("workflows.admin_whatsapp")

_BUTTON_RE = re.compile(r"^(PAID|RESEND|STATUS)[_\-](\S+)$", re.IGNORECASE)


@dataclass
class ActionResult:
    ok: bool
    reply: str


def normalize_whatsapp_number(raw: str) -> str:
    return raw.replace("whatsapp:", "").strip()


def parse_button_payload(body: str, button_payload: str) -> tuple[str, str] | None:
    for candidate in [button_payload, body]:
        candidate = candidate.strip()
        m = _BUTTON_RE.match(candidate)
        if m:
            return m.group(1).upper(), m.group(2)
    reone


def handle_admin_action(
    action: str,
    doc_number: str,
    *,
    qbo_client,
    whatsapp_client,
    settings,
) -> ActionResult:
    try:
        if action == "PAID":
            return _handle_paid(doc_number, qbo_client=qbo_client, settings=settings)
        elif action == "RESEND":
            return _handle_resend(
                doc_number,
                qbo_client=qbo_client,
                whatsapp_client=whatsapp_client,
                settings=settings,
            )
        elif action == "STATUS":
            return _handle_status(doc_number, qbo_client=qbo_client)
        else:
            return ActionResult(ok=False, reply=f"Unknown action: {action}")
    except Exception as exc:
        _LOG.error("admin_action_error",
                   extra={"action": action, "doc_number": doc_number, "error": str(exc)})
        return ActionResult(ok=False, reply=f"Error processing {action}_{doc_number}: {exc}")


def _handle_paid(doc_number: str, *, qbo_client, settings) -> ActionResult:
    from db import payment_links as db
    from workflows.payment_capture import handle_payment_capture, CaptureResult, CaptureError

    row = db.get_by_invoice_number(doc_number)
    if row:
        invoice_id = row["invoice_id"]
        amount = row["amount"]
        currency = row.get("currency", "KWD")
    else:
        invoice = _find_invoice_by_doc_number(doc_number, qbo_client)
        if invoice is None:
            return ActionResult(ok=False, reply=f"Invoice #{doc_number} not found.")
        invoice_id = str(invoice.get("Id", ""))
        amount = float(invoice.get("TotalAmt", 0))
        currency = "KWD"

    result = handle_payment_capture(
        invoice_id, amount, currency,
        qbo_client=qbo_client,
        whatsapp_client=None,
        tap_payment_ref=f"BANK-{doc_number}",
    )

    if isinstance(result, CaptureError):
        if result.reason == "ALREADY_PAID":
            return ActionResult(ok=True, reply=f"Invoice #{doc_number} was already paid ✓")
        return Actionsult(ok=False, reply=f"Failed to mark #{doc_number} paid: {result.detail}")

    return ActionResult(
        ok=True,
        reply=f"✅ Invoice #{doc_number} marked PAID\nAmount: {amount:.3f} {currency}\nQBO Payment ID: {result.qbo_payment_id}",
    )


def _handle_resend(doc_number: str, *, qbo_client, whatsapp_client, settings) -> ActionResult:
    from db import payment_links as db
    from qbo.phone import is_kuwait_mobile, normalize

    if whatsapp_client is None:
        return ActionResult(ok=False, reply="WhatsApp not configured.")

    row = db.get_by_invoice_number(doc_number)
    if row is None:
        return ActionResult(ok=False, reply=f"Invoice #{doc_number} not found in DB.")

    invoice_id = row["invoice_id"]
    payment_url = row["payment_url"]
    amount = row["amount"]
    currency = row.get("currency", "KWD")
    customer_name = row.get("customer_name", "")

    invoice = qbo_client.get_invoice(invoice_id)
    customer_ref = invoice.get("CustomerRef") or {}
    customer_id = str(cuomer_ref.get("value", ""))
    customer = qbo_client.get_customer_by_id(customer_id)

    wa_number = ""
    for raw in [customer.mobile, customer.phone, customer.alternate_phone]:
        norm = normalize(raw)
        if norm and is_kuwait_mobile(norm):
            wa_number = f"+965{norm[4:]}"
            break

    if not wa_number:
        return ActionResult(ok=False, reply=f"No Kuwait mobile found for invoice #{doc_number}.")

    invoice_link = f"https://prime-automation-hub.onrender.com/invoice/{invoice_id}/pdf"
    msg = whatsapp_client.send_payment_link(
        wa_number,
        customer_name=customer_name,
        invoice_number=doc_number,
        amount=amount,
        currency=currency,
        payment_url=payment_url,
        invoice_link=invoice_link,
    )

    if msg.sent:
        return ActionResult(ok=True, reply=f"✅ Payment link resent to {wa_number} for invoice #{doc_number}")
    return ActionResult(ok=False, reply=f"Failed to resend: {msg.error}")


def _handle_status(doc_number:tr, *, qbo_client) -> ActionResult:
    from db import payment_links as db

    row = db.get_by_invoice_number(doc_number)
    if row is None:
        invoice = _find_invoice_by_doc_number(doc_number, qbo_client)
        if invoice is None:
            return ActionResult(ok=False, reply=f"Invoice #{doc_number} not found.")
        balance = float(invoice.get("Balance", 0))
        total = float(invoice.get("TotalAmt", 0))
        status_str = "PAID ✅" if balance <= 0 else "UNPAID ⏳"
        return ActionResult(ok=True, reply=f"Invoice #{doc_number}\nTotal: {total:.3f} KWD\nBalance: {balance:.3f} KWD\nStatus: {status_str}")

    invoice_id = row["invoice_id"]
    invoice = qbo_client.get_invoice(invoice_id)
    balance = float(invoice.get("Balance", 0))
    total = float(invoice.get("TotalAmt", 0))
    status_str = "PAID ✅" if balance <= 0 else "UNPAID ⏳"
    payment_url = row.get("payment_url", "")

    lines = [
        f"Invoice #{doc_number}",
        f"Total: {total:.3f} KWD",
        f"Balancece:.3f} KWD",
        f"Status: {status_str}",
    ]
    if balance > 0 and payment_url:
        lines.append(f"Pay: {payment_url}")

    return ActionResult(ok=True, reply="\n".join(lines))


def _find_invoice_by_doc_number(doc_number: str, qbo_client) -> dict | None:
    try:
        results = qbo_client.query(
            f"SELECT * FROM Invoice WHERE DocNumber = '{doc_number}' MAXRESULTS 1"
        )
        invoices = results.get("QueryResponse", {}).get("Invoice", [])
        return invoices[0] if invoices else None
    except Exception as exc:
        _LOG.error("qbo_invoice_search_error",
                   extra={"doc_number": doc_number, "error": str(exc)})
        return None
