"""QuickBooks webhook signature verification.

Intuit signs each webhook POST with an HMAC-SHA256 digest of the raw request body,
using your Webhook Verifier Token as the key. The result is Base64-encoded and sent
in the `intuit-signature` header.

Critical: always use the raw request bytes — never re-serialize the JSON payload
first, as that may alter byte order or whitespace and break the comparison.
"""
from __future__ import annotations

import base64
import hashlib
import hmac


def compute_signature(payload_bytes: bytes, verifier_token: str) -> tuple[str, str]:
    """Compute the expected intuit-signature (base64) and raw digest hex for the payload."""
    digest = hmac.new(
        verifier_token.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8"), digest.hex()


def verify_signature(
    payload_bytes: bytes,
    signature: str,
    verifier_token: str,
) -> bool:
    """Return True if the intuit-signature header matches the expected HMAC.

    Uses hmac.compare_digest to prevent timing-based side-channel attacks.
    Returns False immediately if the signature or verifier token is empty.
    """
    if not signature or not verifier_token:
        return False
    expected, _ = compute_signature(payload_bytes, verifier_token)
    return hmac.compare_digest(expected, signature)
