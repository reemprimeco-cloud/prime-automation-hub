"""Twilio WhatsApp client for sending payment notifications.

Two templates:
  content_sid               — payment link  (prime_invoice_payment_v2)
  confirmation_content_sid  — payment received (prime_payment_confirmation)

Template variable conventions
------------------------------
Payment link (5 vars):
  {{1}} customer first name
  {{2}} invoice number
  {{3}} amount  e.g. "48.000 KWD"
  {{4}} payment URL
  {{5}} QBO invoice link (empty when missing)

Payment confirmation (3 vars):
  {{1}} customer first name
  {{2}} invoice number
  {{3}} amount  e.g. "48.000 KWD"
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from logging_config import get_logger

_LOG = get_logger("messaging.whatsapp")


@dataclass
class MessageResult:
    to: str
    sid: str = ""
    status: str = ""
    error: str = ""

    @property
    def sent(self) -> bool:
        return bool(self.sid) and not self.error


class WhatsAppClient:
    """Send WhatsApp messages via Twilio Content API templates."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        content_sid: str,
        confirmation_content_sid: str = "",
    ) -> None:
        if not all([account_sid, auth_token, from_number, content_sid]):
            raise ValueError(
                "account_sid, auth_token, from_number, and content_sid are all required."
            )
        from twilio.rest import Client
        self._client = Client(account_sid, auth_token)
        self._from = f"whatsapp:{from_number}"
        self._content_sid = content_sid
        self._confirmation_content_sid = confirmation_content_sid or content_sid

    # ── payment link ──────────────────────────────────────────────────────────

    def send_payment_link(
        self,
        to_number: str,
        *,
        customer_name: str,
        invoice_number: str,
        amount: float,
        currency: str = "KWD",
        payment_url: str,
        invoice_link: str = "",
    ) -> MessageResult:
        """Send payment link template to a customer."""
        variables = {
            "1": customer_name.split()[0],
            "2": invoice_number,
            "3": f"{float(amount):.3f} {currency}",
            "4": payment_url,
            "5": invoice_link if invoice_link else "",
        }
        _LOG.info(
            "whatsapp_payment_link_attempt",
            extra={"to": to_number, "invoice": invoice_number},
        )
        return self._send(to_number, self._content_sid, variables)

    # ── payment confirmation ──────────────────────────────────────────────────

    def send_payment_confirmation(
        self,
        to_number: str,
        *,
        customer_name: str,
        invoice_number: str,
        amount: float,
        currency: str = "KWD",
    ) -> MessageResult:
        """Send payment received confirmation to a customer."""
        variables = {
            "1": customer_name.split()[0],
            "2": invoice_number,
            "3": f"{float(amount):.3f} {currency}",
        }
        _LOG.info(
            "whatsapp_confirmation_attempt",
            extra={"to": to_number, "invoice": invoice_number},
        )
        return self._send(to_number, self._confirmation_content_sid, variables)

    # ── internal ──────────────────────────────────────────────────────────────

    def _send(
        self, to_number: str, content_sid: str, variables: dict
    ) -> MessageResult:
        to = f"whatsapp:{to_number}"
        try:
            msg = self._client.messages.create(
                from_=self._from,
                to=to,
                content_sid=content_sid,
                content_variables=json.dumps(variables),
            )
            _LOG.info(
                "whatsapp_sent",
                extra={"sid": msg.sid, "status": msg.status, "to": to_number},
            )
            return MessageResult(to=to_number, sid=msg.sid, status=msg.status)
        except Exception as exc:
            _LOG.error(
                "whatsapp_send_failed",
                extra={"to": to_number, "error": str(exc)},
            )
            return MessageResult(to=to_number, error=str(exc))


def whatsapp_client_from_settings(settings) -> WhatsAppClient | None:
    """Return a WhatsAppClient from Settings, or None if Twilio isn't configured."""
    if not all([
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.twilio_whatsapp_from,
        settings.twilio_content_sid,
    ]):
        _LOG.warning("whatsapp_not_configured")
        return None
    return WhatsAppClient(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        from_number=settings.twilio_whatsapp_from,
        content_sid=settings.twilio_content_sid,
        confirmation_content_sid=settings.twilio_confirmation_content_sid,
    )
