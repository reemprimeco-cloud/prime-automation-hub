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


def bootstrap_from_env(path: str) -> bool:
    """Seed tokens.json from QBO_TOKENS_JSON environment variable.

    Call this once on server startup (before load_tokens). If QBO_TOKENS_JSON
    is set and tokens.json doesn't exist yet, the env var content is decoded
    and written to disk. Normal read/write then proceeds via the file.

    Returns True if the file was written, False otherwise.
    """
    import base64

    env_val = os.getenv("QBO_TOKENS_JSON", "").strip()
    if not env_val:
        return False
    if os.path.exists(path):
        return False   # file already there — don't overwrite
    try:
        padded = env_val + "=" * (4 - len(env_val) % 4) if len(env_val) % 4 != 0 else env_val
        decoded = base64.b64decode(padded).decode("utf-8")
        json.loads(decoded)  # validate it's parseable before writing
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(decoded)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception:
        return False


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
