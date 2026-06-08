"""Tests for qbo/phone.py — normalization, validation, and per-customer audit.

All tests are purely offline: no QBO credentials, no network. Run with:
    python -m pytest tests/test_phone.py -v
"""
from __future__ import annotations

import pytest
from qbo.phone import AuditResult, audit_customer, is_kuwait_mobile, normalize


# ═══════════════════════════════════════════════════════════════════════════
# normalize()
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalize:
    """All four supported input formats plus edge cases."""

    # ── supported formats ───────────────────────────────────────────────────

    def test_international_with_spaces(self):
        assert normalize("+965 6506 8000") == "+96565068000"

    def test_local_8_digits(self):
        assert normalize("65068000") == "+96565068000"

    def test_international_no_plus(self):
        assert normalize("96565068000") == "+96565068000"

    def test_international_00_prefix(self):
        assert normalize("0096565068000") == "+96565068000"

    # ── already-normalized ──────────────────────────────────────────────────

    def test_already_e164_passthrough(self):
        assert normalize("+96565068000") == "+96565068000"

    # ── formatting variations ────────────────────────────────────────────────

    def test_dashes(self):
        assert normalize("+965-6506-8000") == "+96565068000"

    def test_parentheses(self):
        assert normalize("(965) 6506-8000") == "+96565068000"

    def test_dots(self):
        assert normalize("965.6506.8000") == "+96565068000"

    def test_mixed_formatting(self):
        assert normalize("+965 (650) 6-8000") == "+96565068000"

    def test_extra_whitespace_stripped(self):
        assert normalize("  65068000  ") == "+96565068000"

    # ── extension handling ───────────────────────────────────────────────────

    def test_strips_ext_suffix(self):
        assert normalize("65068000 ext 123") == "+96565068000"

    def test_strips_x_suffix(self):
        assert normalize("65068000 x5") == "+96565068000"

    def test_strips_hash_suffix(self):
        assert normalize("65068000#2") == "+96565068000"

    # ── mobile prefixes all normalize ────────────────────────────────────────

    def test_5_prefix(self):
        assert normalize("50001234") == "+96550001234"

    def test_6_prefix(self):
        assert normalize("60001234") == "+96560001234"

    def test_9_prefix(self):
        assert normalize("90001234") == "+96590001234"

    def test_landline_also_normalizes(self):
        """Landlines normalize but will fail is_kuwait_mobile — that's correct."""
        assert normalize("24123456") == "+96524123456"

    # ── None / empty / garbage ───────────────────────────────────────────────

    def test_none_returns_none(self):
        assert normalize(None) is None

    def test_empty_string_returns_none(self):
        assert normalize("") is None

    def test_whitespace_only_returns_none(self):
        assert normalize("   ") is None

    def test_alpha_only_returns_none(self):
        assert normalize("abc") is None

    def test_too_short_returns_none(self):
        assert normalize("1234") is None

    def test_seven_digits_returns_none(self):
        """7-digit local number is not a valid Kuwait number."""
        assert normalize("6506800") is None

    def test_nine_digits_returns_none(self):
        """9 digits doesn't match local (8) or full (11) patterns."""
        assert normalize("650680001") is None

    def test_wrong_country_code_returns_none(self):
        """UK number — 12 digits, doesn't match any Kuwait pattern."""
        assert normalize("+447911123456") is None

    def test_long_garbage_returns_none(self):
        assert normalize("96565068000123") is None


# ═══════════════════════════════════════════════════════════════════════════
# is_kuwait_mobile()
# ═══════════════════════════════════════════════════════════════════════════

class TestIsKuwaitMobile:

    # ── valid mobile prefixes ─────────────────────────────────────────────────

    @pytest.mark.parametrize("number", [
        "+96565068000",   # 6x — VIVA
        "+96550001234",   # 5x — Zain
        "+96590001234",   # 9x — Ooredoo
        "+96596543210",   # 9x
        "+96559990000",   # 5x
    ])
    def test_valid_mobile(self, number):
        assert is_kuwait_mobile(number) is True

    # ── landlines (starts with 2) ─────────────────────────────────────────────

    @pytest.mark.parametrize("number", [
        "+96524123456",   # 2x — landline
        "+96522345678",   # 22x — government landline
        "+96523000000",
    ])
    def test_landline_rejected(self, number):
        assert is_kuwait_mobile(number) is False

    # ── other invalid patterns ────────────────────────────────────────────────

    def test_none_rejected(self):
        assert is_kuwait_mobile(None) is False

    def test_empty_rejected(self):
        assert is_kuwait_mobile("") is False

    def test_wrong_country_rejected(self):
        assert is_kuwait_mobile("+447911123456") is False

    def test_wrong_length_short_rejected(self):
        assert is_kuwait_mobile("+9656506800") is False   # 7 local digits

    def test_wrong_length_long_rejected(self):
        assert is_kuwait_mobile("+965650680001") is False  # 9 local digits

    def test_non_e164_format_rejected(self):
        assert is_kuwait_mobile("96565068000") is False   # missing leading +

    def test_prefix_1_rejected(self):
        assert is_kuwait_mobile("+96510001234") is False

    def test_prefix_4_rejected(self):
        assert is_kuwait_mobile("+96540001234") is False


# ═══════════════════════════════════════════════════════════════════════════
# audit_customer()
# ═══════════════════════════════════════════════════════════════════════════

class TestAuditCustomer:

    # ── READY scenarios ───────────────────────────────────────────────────────

    def test_ready_from_mobile_field(self):
        r = audit_customer(mobile="65068000")
        assert r.status == "READY"
        assert r.source_field == "Mobile"
        assert r.normalized_phone == "+96565068000"
        assert r.valid_whatsapp is True

    def test_ready_from_primary_phone(self):
        r = audit_customer(mobile="", phone="65068000")
        assert r.status == "READY"
        assert r.source_field == "PrimaryPhone"
        assert r.normalized_phone == "+96565068000"

    def test_ready_from_alternate_phone(self):
        r = audit_customer(mobile="", phone="", alternate_phone="65068000")
        assert r.status == "READY"
        assert r.source_field == "AlternatePhone"

    def test_mobile_preferred_over_primary(self):
        """Mobile wins even when PrimaryPhone is also a valid mobile."""
        r = audit_customer(mobile="65068000", phone="90001234")
        assert r.source_field == "Mobile"
        assert r.normalized_phone == "+96565068000"

    def test_ready_accepts_all_four_input_formats(self):
        for raw in ["+965 6506 8000", "65068000", "96565068000", "0096565068000"]:
            r = audit_customer(mobile=raw)
            assert r.status == "READY", f"Expected READY for {raw!r}"
            assert r.normalized_phone == "+96565068000"

    def test_ready_raw_phone_preserved(self):
        """The exact raw string is stored, not the normalized version."""
        r = audit_customer(mobile="+965 6506 8000")
        assert r.raw_phone == "+965 6506 8000"

    # ── MISSING scenarios ─────────────────────────────────────────────────────

    def test_missing_all_empty(self):
        r = audit_customer()
        assert r.status == "MISSING"
        assert r.valid_whatsapp is False
        assert r.raw_phone == ""
        assert r.normalized_phone == ""

    def test_missing_all_whitespace(self):
        r = audit_customer(mobile="   ", phone=" ", alternate_phone="")
        assert r.status == "MISSING"

    # ── INVALID scenarios ─────────────────────────────────────────────────────

    def test_invalid_landline(self):
        r = audit_customer(phone="24123456")   # 2x = Kuwait landline
        assert r.status == "INVALID"
        assert r.valid_whatsapp is False
        assert r.normalized_phone == "+96524123456"  # normalizes but fails mobile check

    def test_invalid_malformed_number(self):
        r = audit_customer(phone="1234")       # too short to normalize
        assert r.status == "INVALID"
        assert r.normalized_phone == ""        # couldn't normalize

    def test_invalid_shows_best_candidate(self):
        """When mobile is a landline and phone is malformed, use mobile as candidate."""
        r = audit_customer(mobile="24123456", phone="bad")
        assert r.source_field == "Mobile"
        assert r.status == "INVALID"

    def test_invalid_with_some_empty_fields(self):
        """Empty fields are skipped; non-empty landline → INVALID."""
        r = audit_customer(mobile="", phone="24001234", alternate_phone="")
        assert r.status == "INVALID"
        assert r.source_field == "PrimaryPhone"

    # ── status exclusivity ────────────────────────────────────────────────────

    def test_status_values_are_exclusive(self):
        results = [
            audit_customer(mobile="65068000"),
            audit_customer(phone="24123456"),
            audit_customer(),
        ]
        assert [r.status for r in results] == ["READY", "INVALID", "MISSING"]


# ═══════════════════════════════════════════════════════════════════════════
# diagnose_field()
# ═══════════════════════════════════════════════════════════════════════════

from qbo.phone import best_problem, diagnose_field


class TestDiagnoseField:
    """Each problem code plus the valid (None) case."""

    # ── MISSING ──────────────────────────────────────────────────────────────

    def test_none_is_missing(self):
        code, sug = diagnose_field(None)
        assert code == "MISSING"
        assert sug == ""

    def test_empty_string_is_missing(self):
        code, _ = diagnose_field("")
        assert code == "MISSING"

    def test_whitespace_only_is_missing(self):
        code, _ = diagnose_field("   ")
        assert code == "MISSING"

    # ── UNSUPPORTED_FORMAT ────────────────────────────────────────────────────

    def test_alpha_only_is_unsupported(self):
        code, sug = diagnose_field("N/A")
        assert code == "UNSUPPORTED_FORMAT"
        assert sug == ""

    def test_placeholder_text_is_unsupported(self):
        code, _ = diagnose_field("TBD")
        assert code == "UNSUPPORTED_FORMAT"

    def test_dash_only_is_unsupported(self):
        code, _ = diagnose_field("---")
        assert code == "UNSUPPORTED_FORMAT"

    # ── LANDLINE ─────────────────────────────────────────────────────────────

    def test_kuwait_landline_8digit(self):
        code, sug = diagnose_field("24123456")   # local 2x = landline
        assert code == "LANDLINE"
        assert sug == "+96524123456"

    def test_kuwait_landline_with_cc(self):
        code, sug = diagnose_field("96522001234")
        assert code == "LANDLINE"
        assert sug == "+96522001234"

    def test_landline_with_formatting(self):
        code, sug = diagnose_field("2412 3456")
        assert code == "LANDLINE"
        assert sug == "+96524123456"

    def test_landline_suggestion_is_e164(self):
        """Suggestion is always E.164 even for landlines — clean format for records."""
        code, sug = diagnose_field("00965 2400 0000")
        assert code == "LANDLINE"
        assert sug.startswith("+965")
        assert sug == "+96524000000"

    # ── INVALID_COUNTRY ───────────────────────────────────────────────────────

    def test_us_number_11digits(self):
        """11 digits not starting with 965 → INVALID_COUNTRY."""
        code, sug = diagnose_field("12025551234")   # US: 1 + 10 digits
        assert code == "INVALID_COUNTRY"
        assert sug == ""

    def test_saudi_style_11digits(self):
        code, sug = diagnose_field("96650012345")   # Saudi 966 + 8 digits
        assert code == "INVALID_COUNTRY"
        assert sug == ""

    def test_invalid_country_no_suggestion(self):
        """We cannot safely suggest a Kuwait fix for a foreign number."""
        code, sug = diagnose_field("14155552671")   # US 1-415-555-2671
        assert code == "INVALID_COUNTRY"
        assert sug == ""

    # ── INVALID_LENGTH ────────────────────────────────────────────────────────

    def test_too_short_5digits(self):
        code, sug = diagnose_field("12345")
        assert code == "INVALID_LENGTH"
        assert sug == ""

    def test_too_short_7digits(self):
        code, sug = diagnose_field("6506800")    # missing one digit
        assert code == "INVALID_LENGTH"
        assert sug == ""

    def test_too_long_9digits(self):
        code, sug = diagnose_field("650680001")  # one digit too many
        assert code == "INVALID_LENGTH"
        assert sug == ""

    def test_too_long_12digits_foreign(self):
        """12-digit numbers (e.g. UAE +971...) → INVALID_LENGTH (can't infer intent)."""
        code, sug = diagnose_field("+971501234567")
        assert code == "INVALID_LENGTH"
        assert sug == ""

    # ── valid (None code) ─────────────────────────────────────────────────────

    def test_valid_mobile_returns_none_code(self):
        code, sug = diagnose_field("65068000")
        assert code is None
        assert sug == "+96565068000"

    def test_valid_with_cc_returns_none_code(self):
        code, sug = diagnose_field("96565068000")
        assert code is None
        assert sug == "+96565068000"

    def test_valid_with_00_prefix_returns_none(self):
        code, sug = diagnose_field("0096565068000")
        assert code is None
        assert sug == "+96565068000"

    def test_suggestion_always_e164(self):
        """When diagnosis succeeds (code=None or LANDLINE), suggestion is E.164."""
        for raw in ["65068000", "24123456", "+965 6506 8000"]:
            code, sug = diagnose_field(raw)
            if sug:
                assert sug.startswith("+965"), f"Expected E.164 for {raw!r}, got {sug!r}"
                assert len(sug) == 12  # +965 + 8 digits


# ═══════════════════════════════════════════════════════════════════════════
# best_problem()
# ═══════════════════════════════════════════════════════════════════════════

class TestBestProblem:

    def test_landline_beats_invalid_length(self):
        assert best_problem(["INVALID_LENGTH", "LANDLINE"]) == "LANDLINE"

    def test_landline_beats_everything(self):
        all_codes = ["UNSUPPORTED_FORMAT", "INVALID_COUNTRY", "INVALID_LENGTH", "LANDLINE"]
        assert best_problem(all_codes) == "LANDLINE"

    def test_invalid_length_beats_country(self):
        assert best_problem(["INVALID_COUNTRY", "INVALID_LENGTH"]) == "INVALID_LENGTH"

    def test_single_code_returned(self):
        assert best_problem(["MISSING"]) == "MISSING"

    def test_unsupported_is_lowest(self):
        assert best_problem(["UNSUPPORTED_FORMAT"]) == "UNSUPPORTED_FORMAT"
