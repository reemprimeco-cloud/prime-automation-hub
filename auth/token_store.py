"""On-disk persistence for OAuth tokens.

Tokens are written to a JSON file (default: tokens.json, gitignored) with file
permissions locked down to the owner. We track absolute expiry timestamps so the
client can refresh proactively instead of waiting for a 401.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class TokenData:
    access_token: str
    refresh_token: str
    realm_id: str
    access_token_expires_at: float   # epoch seconds
    refresh_token_expires_at: float  # epoch seconds

    def is_access_expired(self, leeway: int = 60) -> bool:
        """True if the access token is expired (or within `leeway` seconds of it)."""
        return time.time() >= (self.access_token_expires_at - leeway)

    def is_refresh_expired(self, leeway: int = 0) -> bool:
        """True if the refresh token itself has expired (re-auth required)."""
        return time.time() >= (self.refresh_token_expires_at - leeway)


def save_tokens(path: str, data: TokenData) -> None:
    """Atomically write tokens to disk with 0600 permissions."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(asdict(data), fh, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best effort (e.g. on Windows); not fatal.
        pass


def load_tokens(path: str) -> Optional[TokenData]:
    """Load tokens from disk, or return None if the file does not exist."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return TokenData(**raw)
