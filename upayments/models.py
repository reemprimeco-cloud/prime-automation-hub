"""UPayments request/response models (provider-agnostic where possible)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PaymentEvent:
    """Normalized payment webhook event (mirrors Tap capture inputs).

    Downstream QBO / WhatsApp logic can consume this without knowing the
    provider. ``status`` is always ``\"paid\"`` or ``\"failed\"``.
    """

    invoice_number: str
    status: str  # "paid" | "failed"
    provider_transaction_id: str
    amount: Optional[float] = None
    currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChargeResult:
    """Result of create_charge — hosted URL + provider transaction id."""

    payment_url: str
    charge_id: str
    status: str = ""
    amount: float = 0.0
    currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "payment_url": self.payment_url,
            "charge_id": self.charge_id,
            "status": self.status,
            "amount": self.amount,
            "currency": self.currency,
            "provider": "upayments",
            "raw": self.raw,
        }
