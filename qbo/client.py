"""A thin QuickBooks Online REST client.

Handles:
  * loading cached tokens,
  * proactive + reactive (401) access-token refresh, persisting rotated tokens,
  * the minorversion query param and JSON Accept headers,
  * structured logging of every request, response, refresh, and error,
  * convenience methods for Company Info, Customers, Invoices, Preferences,
    invoice PDF download, and invoice custom-field discovery.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from config import Settings, get_settings
from auth.oauth import refresh_tokens
from auth.token_store import TokenData, bootstrap_from_env, load_tokens, save_tokens
from logging_config import get_logger
from qbo.models import Customer

_LOG = get_logger("client")


class QBOError(RuntimeError):
    """Raised on a non-success response from the QuickBooks API."""


class NotAuthorizedError(QBOError):
    """Raised when there are no usable tokens (re-run authorization)."""


class QuickBooksClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        bootstrap_from_env(self.settings.token_path)   # no-op if file exists or env var not set
        tokens = load_tokens(self.settings.token_path)
        if tokens is None:
            raise NotAuthorizedError(
                f"No tokens found at '{self.settings.token_path}'. "
                "Run `python -m scripts.authorize` first."
            )
        if self.settings.realm_id:
            tokens.realm_id = self.settings.realm_id
        self.tokens: TokenData = tokens
        self._session = requests.Session()

    # ----- token management -------------------------------------------------

    def _ensure_access_token(self) -> None:
        if self.tokens.is_refresh_expired():
            raise NotAuthorizedError(
                "Refresh token has expired. Re-run `python -m scripts.authorize`."
            )
        if self.tokens.is_access_expired():
            self._refresh()

    def _refresh(self) -> None:
        _LOG.info(
            "token_refresh_start",
            extra={"realm_id": self.tokens.realm_id},
        )
        self.tokens = refresh_tokens(self.settings, self.tokens)
        save_tokens(self.settings.token_path, self.tokens)
        _LOG.info(
            "token_refresh_complete",
            extra={"realm_id": self.tokens.realm_id},
        )

    # ----- low-level request ------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        self._ensure_access_token()
        url = f"{self.settings.api_base_url}{path}"
        all_params: dict[str, Any] = {"minorversion": self.settings.minor_version}
        if params:
            all_params.update(params)

        _LOG.debug(
            "api_request",
            extra={"method": method, "path": path, "params": list(all_params.keys())},
        )
        t0 = time.monotonic()
        resp = self._session.request(
            method, url, headers=self._headers(),
            params=all_params, json=json_body, timeout=30
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 401 and retry_on_401:
            _LOG.warning(
                "token_expired_retrying",
                extra={"method": method, "path": path, "duration_ms": duration_ms},
            )
            self._refresh()
            return self._request(method, path, params=params, retry_on_401=False)

        if resp.status_code >= 400:
            _LOG.error(
                "api_error",
                extra={
                    "method": method,
                    "path": path,
                    "status": resp.status_code,
                    "duration_ms": duration_ms,
                    "body_preview": resp.text[:300],
                },
            )
            raise QBOError(
                f"QuickBooks API error {resp.status_code} on {method} {path}: {resp.text}"
            )

        _LOG.info(
            "api_response",
            extra={"method": method, "path": path, "status": resp.status_code, "duration_ms": duration_ms},
        )
        return resp.json()

    def _base_path(self) -> str:
        return f"/v3/company/{self.tokens.realm_id}"

    # ----- public API -------------------------------------------------------

    def query(self, statement: str) -> dict[str, Any]:
        """Run a QuickBooks SQL-like query and return the raw JSON payload."""
        return self._request(
            "GET", f"{self._base_path()}/query", params={"query": statement}
        )

    def get_company_info(self) -> dict[str, Any]:
        realm = self.tokens.realm_id
        data = self._request("GET", f"{self._base_path()}/companyinfo/{realm}")
        return data.get("CompanyInfo", data)

    def create_payment(
        self,
        *,
        customer_id: str,
        invoice_id: str,
        amount: float,
        tap_charge_id: str = "",
    ) -> dict[str, Any]:
        """Create a QBO payment applied to a specific invoice.

        This marks the invoice as paid in QuickBooks. The payment appears
        in the Receive Payments register and reduces the invoice balance to zero.
        """
        body: dict[str, Any] = {
            "TotalAmt": round(amount, 3),
            "CustomerRef": {"value": customer_id},
            "PaymentMethodRef": {"value": "1000000001"},
            "DepositToAccountRef": {"value": "29"},
            "Line": [
                {
                    "Amount": round(amount, 3),
                    "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
                }
            ],
        }
        if tap_charge_id:
            body["PrivateNote"] = (
                f"Paid via Tap Payments.\nCharge ID: {tap_charge_id}"
            )
        data = self._request("POST", f"{self._base_path()}/payment", json_body=body)
        return data.get("Payment", data)

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Fetch a single invoice by QBO ID.

        Returns the Invoice object (Id, SyncToken, Balance, TotalAmt, CustomerRef, …).
        Raises QBOError with a 404 message if the invoice does not exist.
        """
        data = self._request("GET", f"{self._base_path()}/invoice/{invoice_id}")
        return data.get("Invoice", data)

    def get_invoice_pdf(self, invoice_id: str) -> bytes:
        """Fetch invoice as PDF binary from QBO."""
        self._ensure_access_token()
        url = f"{self.settings.api_base_url}{self._base_path()}/invoice/{invoice_id}/pdf"
        t0 = time.monotonic()
        resp = self._session.get(
            url,
            headers={
                "Authorization": f"Bearer {self.tokens.access_token}",
                "Accept": "application/pdf",
            },
            params={"minorversion": self.settings.minor_version},
            timeout=30,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 401:
            self._refresh()
            return self.get_invoice_pdf(invoice_id)
        if resp.status_code >= 400:
            raise QBOError(
                f"QuickBooks PDF error {resp.status_code} for invoice {invoice_id}: {resp.text}"
            )
        _LOG.info(
            "api_response",
            extra={
                "method": "GET",
                "path": f"/invoice/{invoice_id}/pdf",
                "status": resp.status_code,
                "duration_ms": duration_ms,
            },
        )
        return resp.content

    def update_invoice_note(
        self, invoice_id: str, sync_token: str, private_note: str
    ) -> dict[str, Any]:
        """Append/replace the PrivateNote on a QBO invoice using a sparse update.

        PrivateNote is only visible inside QuickBooks — it does not appear on
        the printed invoice the customer sees.

        QBO requires the current SyncToken to accept any update. Fetch the
        invoice first with get_invoice() to obtain it.
        """
        body: dict[str, Any] = {
            "sparse": True,
            "Id": invoice_id,
            "SyncToken": sync_token,
            "PrivateNote": private_note,
        }
        data = self._request("POST", f"{self._base_path()}/invoice", json_body=body)
        return data.get("Invoice", data)

    def get_preferences(self) -> dict[str, Any]:
        """Fetch the company Preferences entity.

        Contains SalesFormsPrefs.CustomField (legacy custom field definitions)
        and many other company-wide settings.
        """
        data = self._request("GET", f"{self._base_path()}/preferences")
        return data.get("Preferences", data)

    def get_all_active_customers(self, page_size: int = 1000) -> list[Customer]:
        """Return every active customer, paginating automatically.

        Uses STARTPOSITION / MAXRESULTS to walk through companies of any size.
        Customers with Active=false are excluded at the API level.
        """
        all_customers: list[Customer] = []
        start = 1
        while True:
            data = self.query(
                f"SELECT * FROM Customer WHERE Active = true "
                f"STARTPOSITION {start} MAXRESULTS {page_size}"
            )
            batch_raw = (data.get("QueryResponse") or {}).get("Customer") or []
            all_customers.extend(Customer.from_qbo(c) for c in batch_raw)
            _LOG.info(
                "customers_page_fetched",
                extra={"start": start, "count": len(batch_raw)},
            )
            if len(batch_raw) < page_size:
                break
            start += page_size
        return all_customers

    def get_customers(self, max_results: int = 100) -> list[Customer]:
        """Return a list of Customer objects (up to max_results)."""
        data = self.query(f"SELECT * FROM Customer MAXRESULTS {int(max_results)}")
        raw = data.get("QueryResponse", {}).get("Customer") or []
        return [Customer.from_qbo(c) for c in raw]

    def get_customer_by_id(self, customer_id: str | int) -> Customer:
        """Fetch a single customer by QuickBooks ID via the direct REST endpoint.

        Uses GET /v3/company/{realmId}/customer/{id} — no name matching.
        Raises QBOError (wrapping a 404) if the customer does not exist.
        """
        data = self._request("GET", f"{self._base_path()}/customer/{customer_id}")
        raw = data["Customer"]
        customer = Customer.from_qbo(raw)
        return customer

    def get_invoices(
        self, max_results: int = 10, order_by: str = "TxnDate DESC"
    ) -> list[dict[str, Any]]:
        data = self.query(
            f"SELECT * FROM Invoice ORDERBY {order_by} MAXRESULTS {int(max_results)}"
        )
        return data.get("QueryResponse", {}).get("Invoice", [])

    # ----- custom fields ----------------------------------------------------

    def get_invoice_custom_fields(self) -> list[dict[str, Any]]:
        """Discover invoice custom field definitions using three strategies.

        Strategy 1 — Preferences API (legacy fields, works on standard QBO plans)
        Strategy 2 — Invoice query with include=enhancedAllCustomFields (minorversion 75+)
        Strategy 3 — Plain invoice scan (collects fields from existing invoice data)

        Returns a deduplicated list sorted by field ID, each entry containing:
          field_id, field_name, data_type, active, source
        """
        fields: dict[str, dict[str, Any]] = {}

        # Strategy 1: Preferences
        try:
            prefs = self.get_preferences()
            legacy = (
                (prefs.get("SalesFormsPrefs") or {}).get("CustomField") or []
            )
            for cf in legacy:
                fid = str(
                    cf.get("CustomFieldDefinitionId") or cf.get("DefinitionId") or ""
                ).strip()
                if fid:
                    fields[fid] = {
                        "field_id": fid,
                        "field_name": (cf.get("Name") or "").strip(),
                        "data_type": (cf.get("Type") or "").strip(),
                        "active": bool(cf.get("Active", True)),
                        "source": "Preferences",
                    }
            _LOG.info(
                "custom_fields_preferences",
                extra={"found": len(fields)},
            )
        except QBOError as exc:
            _LOG.warning("custom_fields_prefs_failed", extra={"error": str(exc)})

        # Strategies 2 & 3: invoice scan (enhanced then plain)
        for include_param in ("enhancedAllCustomFields", None):
            params: dict[str, Any] = {
                "query": "SELECT * FROM Invoice ORDERBY TxnDate DESC MAXRESULTS 20"
            }
            if include_param:
                params["include"] = include_param
            try:
                data = self._request(
                    "GET", f"{self._base_path()}/query", params=params
                )
            except QBOError as exc:
                _LOG.warning(
                    "custom_fields_invoice_scan_failed",
                    extra={"include": include_param, "error": str(exc)},
                )
                continue

            new_this_pass = 0
            for inv in (data.get("QueryResponse") or {}).get("Invoice") or []:
                for cf in inv.get("CustomField") or []:
                    fid = str(cf.get("DefinitionId") or "").strip()
                    if fid and fid not in fields:
                        tag = "enhanced" if include_param else "standard"
                        fields[fid] = {
                            "field_id": fid,
                            "field_name": (cf.get("Name") or "").strip(),
                            "data_type": (cf.get("Type") or "").strip(),
                            "active": True,
                            "source": f"Invoice ({tag})",
                        }
                        new_this_pass += 1
            _LOG.info(
                "custom_fields_invoice_scan",
                extra={"include": include_param, "new_fields_found": new_this_pass},
            )

        def _sort_key(f: dict) -> tuple:
            fid = f["field_id"]
            return (0, int(fid)) if fid.isdigit() else (1, fid)

        return sorted(fields.values(), key=_sort_key)
