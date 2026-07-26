"""UPayments (UInterface V2) HTTP client.

Sandbox base URL (default):
  https://sandboxapi.upayments.com/api/v1
Live base URL:
  https://uapi.upayments.com/api/v1

Docs:
  - Charge:      POST /charge
  - Status:      GET  /get-payment-status/{track_id}
  - Webhooks:    POST notificationUrl as application/x-www-form-urlencoded
                 Header: x-notification-token
                 Fields: result, track_id, payment_id, requested_order_id, …
  - Auth:        Authorization: Bearer {UPAYMENTS_API_KEY}
                 Accept + Content-Type: application/json (required)

Merchant ID is read from env for config/reconciliation. V2 auth is Bearer-token
based; the merchant id is not used as the primary auth credential.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from logging_config import get_logger
from upayments.exceptions import UPaymentsAPIError
from upayments.models import ChargeResult, PaymentEvent

_LOG = get_logger("upayments.client")

_DEFAULT_BASE_URL = "https://sandboxapi.upayments.com/api/v1"
_PAID_RESULTS = {"CAPTURED", "PAID", "SUCCESS", "success", "captured"}
_FAILED_RESULTS = {
    "NOT CAPTURED",
    "FAILED",
    "CANCELED",
    "CANCELLED",
    "ERROR",
    "DECLINED",
    "failed",
    "canceled",
    "cancelled",
}


def _normalize_customer_name(customer_name: str) -> str:
    """Safe display name for UPayments ``customer.name`` (max 50 chars).

    UPayments takes a single ``name`` field (not first/last). Do **not** apply
    Tap-style first/last splitting or invent a 3-character minimum — empty or
    short names are fine here; we only need a non-empty placeholder.
    """
    name = (customer_name or "").strip()
    if not name:
        return "Customer"
    return name[:50]


def _normalize_phone(customer_phone: str) -> str:
    """UPayments requires ``+`` then digits only (e.g. +96550123456)."""
    raw = (customer_phone or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    return f"+{digits}"


class UPaymentsClient:
    """HTTP client for UPayments UInterface V2."""

    def __init__(
        self,
        api_key: str,
        merchant_id: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        return_url: str = "",
        cancel_url: str = "",
        notification_url: str = "",
        webhook_secret: str = "",
        timeout: int = 30,
    ) -> None:
        if not api_key:
            raise UPaymentsAPIError("UPAYMENTS_API_KEY is required but not set.")
        if not merchant_id:
            raise UPaymentsAPIError("UPAYMENTS_MERCHANT_ID is required but not set.")

        self._api_key = api_key
        self._merchant_id = merchant_id
        self._base_url = base_url.rstrip("/") + "/"
        self._return_url = return_url
        self._cancel_url = cancel_url
        self._notification_url = notification_url
        self._webhook_secret = webhook_secret
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Merchant id is dashboard metadata; included for reconciliation /
            # future gateway requirements without affecting Bearer auth.
            "X-Merchant-Id": str(merchant_id),
        })

    # ── public API ────────────────────────────────────────────────────────────

    def create_charge(
        self,
        amount: float,
        currency: str,
        customer_name: str,
        customer_phone: str,
        invoice_number: str,
        description: str,
        *,
        return_url: str | None = None,
        cancel_url: str | None = None,
        notification_url: str | None = None,
        customer_email: str = "",
        customer_unique_id: str = "",
        language: str = "en",
    ) -> dict[str, Any]:
        """Create a hosted payment link via POST /charge.

        Returns a dict with ``payment_url``, ``charge_id`` (track/payment id),
        and the raw provider response under ``raw``.
        """
        ret = return_url or self._return_url
        cancel = cancel_url or self._cancel_url
        notify = notification_url or self._notification_url
        if not ret or not cancel or not notify:
            raise UPaymentsAPIError(
                "return_url, cancel_url, and notification_url are required "
                "(pass them or set UPAYMENTS_RETURN_URL / UPAYMENTS_CANCEL_URL / "
                "UPAYMENTS_NOTIFICATION_URL)."
            )

        order_id = str(invoice_number).strip().lstrip("#") or f"ORD-{int(time.time())}"
        name = _normalize_customer_name(customer_name)
        mobile = _normalize_phone(customer_phone)

        body: dict[str, Any] = {
            "order": {
                "id": order_id[:40],
                "reference": order_id[:255],
                "description": (description or f"Invoice #{order_id}")[:500],
                "currency": currency.upper(),
                "amount": round(float(amount), 3),
            },
            "language": language[:2] or "en",
            "reference": {"id": order_id[:35]},
            "customer": {
                "uniqueId": (customer_unique_id or order_id)[:50],
                "name": name,
                "email": (customer_email or f"invoice-{order_id}@prime.local")[:50],
            },
            "returnUrl": ret,
            "cancelUrl": cancel,
            "notificationUrl": notify,
            "customerExtraData": f"merchant_id={self._merchant_id};invoice={order_id}",
        }
        if mobile:
            body["customer"]["mobile"] = mobile

        _LOG.info(
            "upayments_create_charge",
            extra={
                "amount": amount,
                "currency": currency,
                "invoice_number": order_id,
                "merchant_id": self._merchant_id,
                "customer_name": name,
            },
        )
        resp_json = self._request("POST", "charge", json_body=body)
        result = self._parse_charge_response(resp_json, amount=amount, currency=currency)
        return result.as_dict()

    def get_charge_status(self, charge_id: str) -> dict[str, Any]:
        """GET /get-payment-status/{track_id} for reconciliation / missed webhooks."""
        if not charge_id:
            raise UPaymentsAPIError("charge_id (track_id) is required.")
        path = f"get-payment-status/{charge_id}"
        _LOG.info("upayments_get_charge_status", extra={"charge_id": charge_id})
        return self._request("GET", path)

    def verify_webhook_signature(self, payload: bytes, signature_header: str) -> bool:
        """Verify UPayments webhook auth when a secret is configured.

        Live notifications send ``x-notification-token`` (static merchant token).
        UPayments FAQ also mentions HMAC rollout; we accept either:

          1. Exact match of header vs ``UPAYMENTS_WEBHOOK_SECRET`` (notification token)
          2. HMAC-SHA256 over the raw body (hex or ``sha256=<hex>``)

        Behavior:
          - If ``UPAYMENTS_WEBHOOK_SECRET`` is empty → True (log warning; token not enforced)
          - If secret is set but header empty → False
          - If secret is set → True when token or HMAC matches
        """
        sig = (signature_header or "").strip()
        secret = (self._webhook_secret or "").strip()

        if not secret:
            _LOG.warning(
                "upayments_webhook_signature_skipped",
                extra={"reason": "UPAYMENTS_WEBHOOK_SECRET not set"},
            )
            return True

        if not sig:
            return False

        # 1) Static notification token (observed live: x-notification-token)
        if hmac.compare_digest(sig, secret):
            return True

        # 2) Optional HMAC-SHA256 over raw body
        expected_hex = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        normalized = sig
        if sig.lower().startswith("sha256="):
            normalized = sig.split("=", 1)[1].strip()

        return hmac.compare_digest(normalized.lower(), expected_hex.lower())

    def parse_webhook_event(self, payload: dict[str, Any]) -> PaymentEvent:
        """Normalize UPayments webhook / redirect fields into PaymentEvent."""
        if not isinstance(payload, dict):
            raise UPaymentsAPIError("Webhook payload must be a dict.")

        # Payload may be flat (webhook docs) or nested under data
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        result_raw = str(
            data.get("result")
            or data.get("payment_status")
            or data.get("status")
            or payload.get("result")
            or ""
        ).strip()
        result_upper = result_raw.upper()

        if result_upper in {r.upper() for r in _PAID_RESULTS} or result_upper == "CAPTURED":
            status = "paid"
        elif result_upper in {r.upper() for r in _FAILED_RESULTS} or "NOT CAPTURED" in result_upper:
            status = "failed"
        elif result_raw.lower() in {"paid", "failed"}:
            status = result_raw.lower()
        else:
            # Unknown → treat as failed so we never auto-mark QBO paid
            status = "failed"
            _LOG.warning(
                "upayments_webhook_unknown_result",
                extra={"result": result_raw},
            )

        invoice_number = str(
            data.get("requested_order_id")
            or data.get("merchant_requested_order_id")
            or data.get("order_id")
            or data.get("orderId")
            or payload.get("requested_order_id")
            or payload.get("orderId")
            or ""
        ).strip()

        provider_txn = str(
            data.get("track_id")
            or data.get("payment_id")
            or data.get("tran_id")
            or data.get("receipt_id")
            or data.get("order_id")
            or ""
        ).strip()

        amount: Optional[float] = None
        for key in ("amount", "total_price", "paid", "transaction_amount"):
            if data.get(key) is not None and data.get(key) != "":
                try:
                    amount = float(data[key])
                    break
                except (TypeError, ValueError):
                    continue

        currency = str(data.get("currency") or payload.get("currency") or "").upper()

        return PaymentEvent(
            invoice_number=invoice_number,
            status=status,
            provider_transaction_id=provider_txn,
            amount=amount,
            currency=currency,
            raw=dict(payload),
        )

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return urljoin(self._base_url, path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._url(path)
        try:
            resp = self._session.request(
                method,
                url,
                json=json_body,
                timeout=self._timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise UPaymentsAPIError(f"UPayments request failed: {exc}") from exc

        body_text = resp.text[:2000] if resp.text else ""
        if not resp.ok:
            raise UPaymentsAPIError(
                f"UPayments HTTP {resp.status_code} on {method} {path}: {body_text}",
                status_code=resp.status_code,
                body=body_text,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise UPaymentsAPIError(
                f"UPayments returned non-JSON on {method} {path}: {body_text}",
                status_code=resp.status_code,
                body=body_text,
            ) from exc

        # Many UPayments endpoints wrap with {"status": true/false, "data": …}
        if isinstance(data, dict) and data.get("status") is False:
            msg = data.get("message") or data.get("error") or body_text or "status=false"
            raise UPaymentsAPIError(
                f"UPayments API error on {method} {path}: {msg}",
                status_code=resp.status_code,
                body=body_text,
            )
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _parse_charge_response(
        data: dict[str, Any],
        *,
        amount: float,
        currency: str,
    ) -> ChargeResult:
        nested = data.get("data") if isinstance(data.get("data"), dict) else data

        payment_url = str(
            nested.get("link")
            or nested.get("paymentURL")
            or nested.get("payment_url")
            or nested.get("paymentUrl")
            or nested.get("redirect_url")
            or nested.get("url")
            or ""
        ).strip()

        charge_id = str(
            nested.get("track_id")
            or nested.get("payment_id")
            or nested.get("invoice_id")
            or nested.get("order_id")
            or nested.get("id")
            or ""
        ).strip()

        if not payment_url:
            raise UPaymentsAPIError(
                f"UPayments charge response missing payment URL: {str(data)[:300]}",
                body=str(data)[:500],
            )
        if not charge_id and payment_url:
            # Live charge responses often return only data.link?session_id=…
            qs = parse_qs(urlparse(payment_url).query)
            charge_id = str((qs.get("session_id") or [""])[0]).strip()
        if not charge_id:
            charge_id = payment_url

        status = str(nested.get("result") or nested.get("status") or data.get("message") or "")
        return ChargeResult(
            payment_url=payment_url,
            charge_id=charge_id,
            status=status,
            amount=float(nested.get("amount", amount) or amount),
            currency=str(nested.get("currency") or currency).upper(),
            raw=data,
        )


def upayments_client_from_env(*, webhook_only: bool = False) -> UPaymentsClient:
    """Build a client from environment variables (sandbox-friendly defaults).

    Set ``webhook_only=True`` for notification handling when charge credentials
    are not configured yet — only ``UPAYMENTS_WEBHOOK_SECRET`` is needed then.
    """
    api_key = os.getenv("UPAYMENTS_API_KEY", "").strip()
    merchant_id = os.getenv("UPAYMENTS_MERCHANT_ID", "").strip()
    if webhook_only:
        api_key = api_key or "webhook-only"
        merchant_id = merchant_id or "0"
    return UPaymentsClient(
        api_key=api_key,
        merchant_id=merchant_id,
        base_url=os.getenv("UPAYMENTS_BASE_URL", _DEFAULT_BASE_URL).strip()
        or _DEFAULT_BASE_URL,
        return_url=os.getenv("UPAYMENTS_RETURN_URL", "").strip(),
        cancel_url=os.getenv("UPAYMENTS_CANCEL_URL", "").strip(),
        notification_url=os.getenv("UPAYMENTS_NOTIFICATION_URL", "").strip(),
        webhook_secret=os.getenv("UPAYMENTS_WEBHOOK_SECRET", "").strip(),
    )
