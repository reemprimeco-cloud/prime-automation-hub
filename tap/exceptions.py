"""Tap Payments exception hierarchy.

TapError              base class
  TapAuthError        401 — bad API key, re-auth needed
  TapClientError      4xx — bad request, don't retry
  TapRateLimitError   429 — slow down
  TapServerError      5xx — transient, retry allowed
  TapParseError       response parsing failed
"""
from __future__ import annotations


class TapError(RuntimeError):
    """Base for all Tap Payments errors."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class TapAuthError(TapError):
    """401 — the secret key is invalid or expired."""


class TapClientError(TapError):
    """4xx (excluding 401 and 429) — do not retry."""


class TapRateLimitError(TapError):
    """429 — retry with a longer back-off."""


class TapServerError(TapError):
    """5xx — transient server-side error, retry is appropriate."""


class TapParseError(TapError):
    """The response could not be parsed into the expected shape."""
