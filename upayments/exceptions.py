"""UPayments API exceptions."""
from __future__ import annotations


class UPaymentsAPIError(RuntimeError):
    """Raised on non-success responses or client configuration errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
