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


def decode_qbo_tokens_env() -> str:
    """Decode QBO_TOKENS_JSON from Render env (base64 or raw JSON)."""
    import base64
    import re

    env_val = os.getenv("QBO_TOKENS_JSON", "").strip()
    if not env_val:
        raise ValueError("QBO_TOKENS_JSON is empty — set it in Render → Environment")

    # Render / copy-paste sometimes wraps the value in quotes
    if (env_val.startswith('"') and env_val.endswith('"')) or (
        env_val.startswith("'") and env_val.endswith("'")
    ):
        env_val = env_val[1:-1].strip()

    if env_val.startswith("{"):
        decoded = env_val
    else:
        # Keep only base64 alphabet; drop newlines/spaces/smart junk from paste
        cleaned = re.sub(r"[^A-Za-z0-9+/=_-]", "", env_val)
        if not cleaned:
            raise ValueError("QBO_TOKENS_JSON base64 is empty after cleanup")
        # url-safe → standard
        cleaned = cleaned.replace("-", "+").replace("_", "/")
        pad = (-len(cleaned)) % 4
        if pad:
            cleaned += "=" * pad
        try:
            decoded = base64.b64decode(cleaned, validate=False).decode("utf-8")
        except Exception as exc:
            raise ValueError(
                f"QBO_TOKENS_JSON is not valid base64 ({exc}). "
                f"Length={len(env_val)}. Prefer pasting raw tokens.json "
                f"(starts with {{) instead of base64."
            ) from exc

    try:
        raw = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"QBO_TOKENS_JSON decoded but is not valid JSON ({exc}). "
            "Paste was likely truncated — re-copy the full tokens.json from Mac."
        ) from exc

    for key in ("access_token", "refresh_token", "realm_id"):
        if not raw.get(key):
            raise ValueError(f"tokens JSON missing required field: {key}")
    return decoded


def bootstrap_from_env(path: str, *, force: bool = False) -> bool:
    """Seed tokens.json from QBO_TOKENS_JSON environment variable.

    Call this once on server startup (before load_tokens). If QBO_TOKENS_JSON
    is set and tokens.json doesn't exist yet (or force=True), the env var
    content is decoded and written to disk. Normal read/write then proceeds
    via the file.

    Returns True if the file was written, False otherwise.
    """
    import base64

    from logging_config import get_logger

    _log = get_logger("auth.token_store")
    env_val = os.getenv("QBO_TOKENS_JSON", "").strip()
    if not env_val:
        return False
    if os.path.exists(path) and not force:
        return False   # file already there — don't overwrite
    try:
        decoded = decode_qbo_tokens_env()
        json.loads(decoded)  # validate
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(decoded)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception as exc:
        _log.error("qbo_tokens_bootstrap_failed", extra={"error": str(exc), "path": path})
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
