"""Kuwait phone number normalization and WhatsApp eligibility validation.

Normalization
-------------
Accepts numbers in any of these formats and returns E.164 (+965XXXXXXXX):

  +965 6506 8000   international with spaces/formatting
  65068000         8-digit local (no country code)
  96565068000      international, no leading +
  0096565068000    international, 00 prefix

WhatsApp eligibility (Kuwait mobile)
--------------------------------------
A number is considered a valid WhatsApp-capable Kuwait mobile when:
  * It normalizes successfully to +965XXXXXXXX
  * The first digit of the local 8-digit part is 5, 6, or 9
    (Zain: 5x/9x, Ooredoo: 9x/5x, VIVA: 6x/9x)

Landlines (first local digit 2) and special/service numbers are rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── constants ───────────────────────────────────────────────────────────────

KUWAIT_CC = "965"
E164_PREFIX = "+965"
LOCAL_LEN = 8           # digits after country code
FULL_LEN = 11           # 965 + 8 local digits

# Prefixes indicating mobile service in Kuwait (Zain, Ooredoo, VIVA)
MOBILE_PREFIXES: frozenset[str] = frozenset("569")

# Pattern strips trailing extensions before normalizing (e.g. "ext 123", "x12", "#3")
_EXT_RE = re.compile(r"(?i)\s*(ext|x|#)\.?\s*\d+\s*$")

# ── normalization ────────────────────────────────────────────────────────────

def normalize(raw: str | None) -> str | None:
    """Normalize any Kuwait phone number string to E.164 (+965XXXXXXXX).

    Returns None if the input cannot be interpreted as a Kuwait number.
    """
    if not raw:
        return None

    # Strip trailing extension annotations before anything else
    cleaned = _EXT_RE.sub("", raw).strip()

    # Extract only digits
    digits = re.sub(r"\D", "", cleaned)
    if not digits:
        return None

    # Strip international dialing prefix (00...)
    if digits.startswith("00"):
        digits = digits[2:]

    # Case 1: already a full international number (11 digits, 965...)
    if len(digits) == FULL_LEN and digits.startswith(KUWAIT_CC):
        return f"+{digits}"

    # Case 2: local 8-digit number (no country code)
    if len(digits) == LOCAL_LEN:
        return f"+{KUWAIT_CC}{digits}"

    # Anything else cannot be reliably interpreted
    return None


# ── validation ───────────────────────────────────────────────────────────────

def is_kuwait_mobile(normalized: str | None) -> bool:
    """Return True iff the normalized number is a Kuwait mobile (WhatsApp-eligible).

    Criteria:
      * Must start with +965
      * Must have exactly 8 local digits
      * First local digit must be 5, 6, or 9 (mobile operator prefixes)
    """
    if not normalized:
        return False
    if not normalized.startswith(E164_PREFIX):
        return False
    local = normalized[len(E164_PREFIX):]
    if len(local) != LOCAL_LEN or not local.isdigit():
        return False
    return local[0] in MOBILE_PREFIXES


# ── problem diagnosis ─────────────────────────────────────────────────────────

# Ordered by actionability (most-recoverable first) — used when aggregating
# per-field problems to a single customer-level code.
PROBLEM_PRIORITY: tuple[str, ...] = (
    "LANDLINE",           # Kuwait number but not mobile — ask for mobile
    "INVALID_LENGTH",     # wrong digit count — likely a typo
    "INVALID_COUNTRY",    # foreign country code entered — clarify
    "UNSUPPORTED_FORMAT", # non-numeric or empty-ish — needs re-entry
    "MISSING",            # field absent entirely
)


def diagnose_field(raw: str | None) -> tuple[str | None, str]:
    """Diagnose a single phone-field value and suggest a fix when possible.

    Returns
    -------
    (problem_code, suggestion)
        problem_code  None means the number is actually valid.
                      Otherwise one of PROBLEM_PRIORITY.
        suggestion    E.164 string if the number can be normalized (even a
                      landline), otherwise "".

    Decision tree
    -------------
    0 digits after cleanup          → UNSUPPORTED_FORMAT
    8 digits                        → Kuwait local
      └ not mobile prefix           → LANDLINE      suggestion = E.164
    11 digits, starts with 965      → Kuwait full
      └ not mobile prefix           → LANDLINE      suggestion = E.164
    11 digits, other country code   → INVALID_COUNTRY
    any other digit count           → INVALID_LENGTH
    """
    if not raw or not raw.strip():
        return ("MISSING", "")

    # Strip extension annotations before extracting digits
    cleaned = _EXT_RE.sub("", raw).strip()
    digits = re.sub(r"\D", "", cleaned)

    if not digits:
        return ("UNSUPPORTED_FORMAT", "")

    # Strip international dialing prefix
    if digits.startswith("00"):
        digits = digits[2:]

    if not digits:
        return ("UNSUPPORTED_FORMAT", "")

    # 8-digit local number — assumed Kuwait
    if len(digits) == LOCAL_LEN:
        norm = f"+{KUWAIT_CC}{digits}"
        if is_kuwait_mobile(norm):
            return (None, norm)          # valid mobile
        return ("LANDLINE", norm)        # Kuwait landline

    # 11-digit with country code
    if len(digits) == FULL_LEN:
        if digits.startswith(KUWAIT_CC):
            norm = f"+{digits}"
            if is_kuwait_mobile(norm):
                return (None, norm)      # valid mobile
            return ("LANDLINE", norm)    # Kuwait landline
        return ("INVALID_COUNTRY", "")  # foreign country code

    # Wrong digit count
    return ("INVALID_LENGTH", "")


def best_problem(codes: list[str]) -> str:
    """Return the highest-priority problem code from a list."""
    for p in PROBLEM_PRIORITY:
        if p in codes:
            return p
    return "UNSUPPORTED_FORMAT"  # fallback (should not normally reach here)


@dataclass
class AuditResult:
    """Outcome of a single customer's phone audit."""
    source_field: str    # "Mobile" | "PrimaryPhone" | "AlternatePhone" | ""
    raw_phone: str       # the raw QBO value that was selected
    normalized_phone: str  # E.164 string, or "" if normalization failed
    valid_whatsapp: bool
    status: str          # "READY" | "MISSING" | "INVALID"


# Field check order: Mobile is tried first (most likely to be a cell number),
# then PrimaryPhone, then AlternatePhone.
_FIELD_ORDER = ("Mobile", "PrimaryPhone", "AlternatePhone")


def audit_customer(
    mobile: str = "",
    phone: str = "",
    alternate_phone: str = "",
) -> AuditResult:
    """Determine the best WhatsApp-ready number for a customer.

    Selection logic
    ---------------
    1. Try each field in priority order (Mobile → PrimaryPhone → AlternatePhone).
    2. The first field that normalizes *and* passes is_kuwait_mobile() → READY.
    3. If none pass: report INVALID for the first non-empty field.
    4. If all are empty: MISSING.
    """
    fields: dict[str, str] = {
        "Mobile": (mobile or "").strip(),
        "PrimaryPhone": (phone or "").strip(),
        "AlternatePhone": (alternate_phone or "").strip(),
    }

    # Pass 1 — look for a valid Kuwait mobile
    for field_name in _FIELD_ORDER:
        raw = fields[field_name]
        if not raw:
            continue
        norm = normalize(raw)
        if norm and is_kuwait_mobile(norm):
            return AuditResult(
                source_field=field_name,
                raw_phone=raw,
                normalized_phone=norm,
                valid_whatsapp=True,
                status="READY",
            )

    # Pass 2 — no valid mobile found; report the best candidate (INVALID)
    for field_name in _FIELD_ORDER:
        raw = fields[field_name]
        if not raw:
            continue
        norm = normalize(raw) or ""
        return AuditResult(
            source_field=field_name,
            raw_phone=raw,
            normalized_phone=norm,
            valid_whatsapp=False,
            status="INVALID",
        )

    # All fields empty
    return AuditResult(
        source_field="",
        raw_phone="",
        normalized_phone="",
        valid_whatsapp=False,
        status="MISSING",
    )
