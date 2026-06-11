"""Clean dataclasses for Tap Payments API request and response objects."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TapPhoneNumber:
    """Country code and local number, split as Tap expects them."""
    country_code: str   # "965"
    number: str         # "65068000" — 8 digits, no leading country code


@dataclass
class TapCustomer:
    first_name: str
    last_name: str
    email: str
    phone: TapPhoneNumber
    tap_customer_id: str = ""   # pre-existing Tap customer ID, if any


@dataclass
class CreateChargeRequest:
    """All data needed to create a hosted payment link via Tap."""
    amount: float
    currency: str                   # "KWD"
    description: str                # e.g. "Invoice #1089 — Acme Corp"
    customer: TapCustomer
    # reference fields stored in the Tap charge for reconciliation
    transaction_ref: str            # e.g. "INV-1089"
    order_ref: str                  # QBO invoice ID, e.g. "42"
    # URLs
    redirect_url: str               # customer lands here after paying
    webhook_url: str                # Tap POSTs the result here
    # metadata forwarded to Tap (udf1–udf5, free text)
    metadata: dict[str, str]
    # checkout session expiry (minutes, 5–60 allowed by Tap)
    expiry_minutes: int = 60


@dataclass
class InvoiceResponse:
    """Parsed response from Tap's create-invoice endpoint."""
    id: str                 # e.g. "inv_kN0e13110124xGi527019"
    status: str             # "CREATED" for a fresh invoice link
    url: str                # hosted invoice URL to share with the customer
    amount: float = 0.0
    currency: str = ""
    tap_customer_id: str = ""


@dataclass
class ChargeResponse:
    """Parsed response from Tap's create-charge endpoint."""
    charge_id: str          # e.g. "chg_TS07A5020231643Obe10906052"
    payment_url: str        # the hosted checkout URL to share with the customer
    status: str             # "INITIATED" for a fresh link
    amount: float
    currency: str
    tap_customer_id: str    # Tap assigns/returns a customer ID
