"""Domain models for QuickBooks entities.

Each model stores the specific fields required by this integration and provides
a `from_qbo` classmethod that safely maps from the raw QBO API JSON response.
"""
from __future__ import annotations

from dataclasses import dataclass


def _nested(data: dict, outer_key: str, inner_key: str) -> str:
    """Safely extract a nested field like PrimaryEmailAddr.Address.

    Handles absent keys, explicit None values, and empty strings uniformly.
    """
    outer = data.get(outer_key)
    if not isinstance(outer, dict):
        return ""
    return (outer.get(inner_key) or "").strip()


@dataclass
class Customer:
    """Represents a QuickBooks customer with the fields required by this integration."""

    id: str               # QuickBooks internal ID — use this for all ID-based lookups
    display_name: str
    email: str            # PrimaryEmailAddr.Address
    phone: str            # PrimaryPhone.FreeFormNumber
    mobile: str           # Mobile.FreeFormNumber
    alternate_phone: str  # AlternatePhone.FreeFormNumber

    @classmethod
    def from_qbo(cls, data: dict) -> "Customer":
        """Build a Customer from a raw QBO Customer JSON object."""
        return cls(
            id=str(data["Id"]),
            display_name=(data.get("DisplayName") or "").strip(),
            email=_nested(data, "PrimaryEmailAddr", "Address"),
            phone=_nested(data, "PrimaryPhone", "FreeFormNumber"),
            mobile=_nested(data, "Mobile", "FreeFormNumber"),
            alternate_phone=_nested(data, "AlternatePhone", "FreeFormNumber"),
        )
