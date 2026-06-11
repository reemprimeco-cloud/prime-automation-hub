"""Tap Payments API client.

Wraps Tap v2 invoices and charges endpoints with:
  * Bearer-token authentication
  * Structured request/response logging (no secret values leaked)
  * Exponential-backoff retry for transient server errors (5xx) and network failures
  * No retry on client errors (4xx) — those require fixing the request
  * Idempotency: callers pass in a stable request; this client does not add
    its own idempotency keys (Tap does not support them on the charges endpoint)

Usage
-----
    from tap.client import TapClient, tap_client_from_settings
    from config import get_settings

    client = tap_client_from_settings(get_settings())
    response = client.create_tap_invoice(request)
    print(response.url)
"""
from __future__ import annotations

import time
from typing import Any

import requests

from config import Settings
from logging_config import get_logger
from tap.exceptions import (
    TapAuthError, TapClientError, TapError,
    TapParseError, TapRateLimitError, TapServerError,
)
from tap.models import ChargeResponse, CreateChargeRequest, InvoiceResponse

_LOG = get_logger("tap.client")

_BASE_URL = "https://api.tap.company/v2"
_MAX_RETRIES = 3
_RETRYABLE_STATUS = {500, 502, 503, 504}


class TapClient:
    """HTTP client for the Tap Payments v2 API."""

    def __init__(self, secret_key: str, *, timeout: int = 30) -> None:
        if not secret_key:
            raise TapError("TAP_SECRET_KEY is required but not set.")
        self._secret_key = secret_key
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {secret_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # ── public API ────────────────────────────────────────────────────────────

    def create_tap_invoice(self, req: CreateChargeRequest) -> InvoiceResponse:
        """Create a Tap invoice with a hosted payment URL (7-day due/expiry).

        Returns an InvoiceResponse with status='CREATED' and a valid url.
        Raises TapError (or a subclass) on any failure.
        """
        body = self._build_invoice_body(req)
        _LOG.info(
            "tap_create_invoice",
            extra={
                "amount": req.amount,
                "currency": req.currency,
                "description": req.description,
                "transaction_ref": req.transaction_ref,
                "order_ref": req.order_ref,
            },
        )
        resp_json = self._post("/invoices", body)
        return self._parse_invoice_response(resp_json)

    def create_charge(self, req: CreateChargeRequest) -> ChargeResponse:
        """Create a hosted payment link.

        Returns a ChargeResponse with status='INITIATED' and a valid payment_url.
        Raises TapError (or a subclass) on any failure.
        """
        body = self._build_charge_body(req)
        _LOG.info(
            "tap_create_charge",
            extra={
                "amount": req.amount,
                "currency": req.currency,
                "description": req.description,
                "transaction_ref": req.transaction_ref,
                "order_ref": req.order_ref,
            },
        )
        resp_json = self._post("/charges", body)
        return self._parse_charge_response(resp_json)

    def get_charge(self, charge_id: str) -> dict[str, Any]:
        """Retrieve an existing charge by ID."""
        url = f"{_BASE_URL}/charges/{charge_id}"
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            raise TapError(f"Tap retrieve charge failed: {exc}") from exc

        if not resp.ok:
            self._raise_client_error(resp)

        try:
            return resp.json()
        except ValueError as exc:
            raise TapParseError(f"Tap returned non-JSON: {resp.text[:200]}") from exc

    def get_tap_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Retrieve an existing Tap invoice by ID."""
        url = f"{_BASE_URL}/invoices/{invoice_id}"
        try:
            resp = self._session.get(url, timeout=self._timeout)
        except requests.exceptions.RequestException as exc:
            raise TapError(f"Tap retrieve invoice failed: {exc}") from exc

        if not resp.ok:
            self._raise_client_error(resp)

        try:
            return resp.json()
        except ValueError as exc:
            raise TapParseError(f"Tap returned non-JSON: {resp.text[:200]}") from exc

    # ── request building ──────────────────────────────────────────────────────

    @staticmethod
    def _build_invoice_body(req: CreateChargeRequest) -> dict[str, Any]:
        expiry_ms = int((time.time() + 7 * 24 * 3600) * 1000)
        body: dict[str, Any] = {
            "draft": False,
            "due": expiry_ms,
            "expiry": expiry_ms,
            "description": req.description,
            "mode": "INVOICE",
            "currencies": [req.currency],
            "metadata": req.metadata,
            "customer": {
                "first_name": req.customer.first_name,
                "last_name": req.customer.last_name,
                "email": req.customer.email,
            },
            "order": {
                "amount": round(req.amount, 3),
                "currency": req.currency,
            },
            "redirect": {"url": req.redirect_url},
            "post": {"url": req.webhook_url},
            "reference": {
                "invoice": req.transaction_ref,
                "order": req.order_ref,
            },
            "notifications": {
                "channels": [],
                "dispatch": False,
            },
        }

        ph = req.customer.phone
        if ph.country_code and ph.number:
            body["customer"]["phone"] = {
                "country_code": ph.country_code,
                "number": ph.number,
            }

        if req.customer.tap_customer_id:
            body["customer"]["id"] = req.customer.tap_customer_id

        return body

    @staticmethod
    def _build_charge_body(req: CreateChargeRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "amount": round(req.amount, 3),
            "currency": req.currency,
            "customer_initiated": True,
            "threeDSecure": True,
            "save_card": False,
            "description": req.description,
            "statement_descriptor": "PRIME",
            "metadata": req.metadata,
            "reference": {
                "transaction": req.transaction_ref,
                "order": req.order_ref,
            },
            "receipt": {"email": False, "sms": False},
            "customer": {
                "first_name": req.customer.first_name,
                "last_name":  req.customer.last_name,
                "email":      req.customer.email,
            },
            "source": {"id": "src_all"},
            "redirect": {"url": req.redirect_url},
            "post":     {"url": req.webhook_url},
            "transaction": {
                "expiry": {
                    "period": 60,
                    "type": "MINUTE",
                }
            },
        }

        # Include phone only when both parts are present
        ph = req.customer.phone
        if ph.country_code and ph.number:
            body["customer"]["phone"] = {
                "country_code": ph.country_code,
                "number": ph.number,
            }

        # Include existing Tap customer ID if available
        if req.customer.tap_customer_id:
            body["customer"]["id"] = req.customer.tap_customer_id

        return body

    # ── response parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_charge_response(data: dict[str, Any]) -> ChargeResponse:
        charge_id = data.get("id", "")
        if not charge_id:
            raise TapParseError(f"Tap response missing 'id' field: {str(data)[:200]}")

        status = data.get("status", "")
        transaction = data.get("transaction") or {}
        payment_url = transaction.get("url", "")

        if status == "INITIATED" and not payment_url:
            raise TapParseError(
                f"Tap returned INITIATED status for charge {charge_id} "
                "but did not include transaction.url"
            )

        _LOG.info(
            "tap_charge_created",
            extra={
                "charge_id": charge_id,
                "status": status,
                "has_url": bool(payment_url),
            },
        )

        customer_data = data.get("customer") or {}
        return ChargeResponse(
            charge_id=charge_id,
            payment_url=payment_url,
            status=status,
            amount=float(data.get("amount", 0)),
            currency=data.get("currency", ""),
            tap_customer_id=customer_data.get("id", ""),
        )

    @staticmethod
    def _parse_invoice_response(data: dict[str, Any]) -> InvoiceResponse:
        invoice_id = data.get("id", "")
        if not invoice_id:
            raise TapParseError(f"Tap response missing 'id' field: {str(data)[:200]}")

        status = data.get("status", "")
        url = data.get("url", "")

        if status in {"CREATED", "SAVED"} and not url:
            raise TapParseError(
                f"Tap returned {status} status for invoice {invoice_id} "
                "but did not include url"
            )

        _LOG.info(
            "tap_invoice_created",
            extra={
                "invoice_id": invoice_id,
                "status": status,
                "has_url": bool(url),
            },
        )

        customer_data = data.get("customer") or {}
        order = data.get("order") or {}
        return InvoiceResponse(
            id=invoice_id,
            status=status,
            url=url,
            amount=float(order.get("amount", data.get("amount", 0))),
            currency=str(order.get("currency", data.get("currency", ""))),
            tap_customer_id=customer_data.get("id", ""),
        )

    # ── HTTP layer with retry ─────────────────────────────────────────────────

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{_BASE_URL}{path}"
        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            if attempt > 0:
                delay = 2 ** (attempt - 1)   # 1s, 2s, 4s
                _LOG.warning(
                    "tap_retry",
                    extra={"attempt": attempt, "delay_s": delay, "path": path},
                )
                time.sleep(delay)

            try:
                resp = self._session.post(url, json=body, timeout=self._timeout)
            except requests.exceptions.ConnectionError as exc:
                _LOG.warning("tap_connection_error", extra={"error": str(exc)})
                last_exc = exc
                continue
            except requests.exceptions.Timeout as exc:
                _LOG.warning("tap_timeout", extra={"path": path})
                last_exc = exc
                continue

            _LOG.debug(
                "tap_response",
                extra={"status": resp.status_code, "path": path},
            )

            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                _LOG.warning("tap_rate_limited", extra={"retry_in_s": wait})
                if attempt < _MAX_RETRIES:
                    time.sleep(wait)
                    continue
                raise TapRateLimitError(
                    "Tap rate limit exceeded after retries",
                    status_code=429,
                    body=resp.text,
                )

            if resp.status_code in _RETRYABLE_STATUS:
                last_exc = TapServerError(
                    f"Tap server error {resp.status_code}",
                    status_code=resp.status_code,
                    body=resp.text[:300],
                )
                continue

            if not resp.ok:
                self._raise_client_error(resp)

            try:
                return resp.json()
            except ValueError as exc:
                raise TapParseError(
                    f"Tap returned non-JSON: {resp.text[:200]}"
                ) from exc

        # All retries exhausted
        if isinstance(last_exc, Exception):
            if isinstance(last_exc, TapError):
                raise last_exc
            raise TapError(f"Tap request failed after {_MAX_RETRIES} retries: {last_exc}") from last_exc
        raise TapError("Tap request failed for unknown reason")

    @staticmethod
    def _raise_client_error(resp: requests.Response) -> None:
        try:
            data = resp.json()
            errors = data.get("errors") or []
            desc = "; ".join(
                e.get("description", "") or e.get("message", "")
                for e in errors
            ) or resp.text[:300]
        except ValueError:
            desc = resp.text[:300]

        if resp.status_code == 401:
            raise TapAuthError(
                f"Tap authentication failed: {desc}",
                status_code=401, body=desc,
            )
        raise TapClientError(
            f"Tap client error {resp.status_code}: {desc}",
            status_code=resp.status_code, body=desc,
        )


def tap_client_from_settings(settings: Settings) -> TapClient:
    """Construct a TapClient from application Settings."""
    if not settings.tap_secret_key:
        raise TapError(
            "TAP_SECRET_KEY is not configured. "
            "Add it to your .env file."
        )
    return TapClient(settings.tap_secret_key)
