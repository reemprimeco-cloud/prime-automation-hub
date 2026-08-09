"""Central configuration, loaded entirely from environment variables.

All credentials live in the environment (via a .env file in development) — never
hard-coded. Call `get_settings()` to obtain a validated, immutable Settings object.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load variables from a local .env file if present. In production you may instead
# inject these through your process manager / secrets store.
load_dotenv()

VALID_ENVIRONMENTS = {"sandbox", "production"}

API_BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    environment: str
    minor_version: str
    token_path: str  # Path to tokens.json. For Render persistent disk, set QBO_TOKEN_PATH=/data/tokens.json or
                     # equivalent (e.g. /opt/render/project/data/tokens.json depending on mount point in Render dashboard).
                     # Relative paths work locally; absolute paths recommended for Render to survive redeploys.
    realm_id: str | None
    webhook_verifier_token: str | None  # required only for the webhook receiver
    # ── Phase 3: Tap Payments + database (default values keep existing tests green) ──
    tap_secret_key: str | None = None
    tap_redirect_url: str = "http://localhost:8080/payment/success"
    tap_webhook_url: str = "http://localhost:8000/webhook/tap"
    database_path: str = "hub.db"
    # ── Phase 4: Twilio WhatsApp ──────────────────────────────────────────────
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_whatsapp_from: str = ""   # E.164 approved sender, e.g. "+96565xxxxxx"
    twilio_content_sid: str = ""     # HX... payment link template SID
    twilio_confirmation_content_sid: str = ""  # HX... payment confirmation template SID
    qbo_deposit_account_id: str | None = None  # bank / undeposited funds account
    bank_transfer_info: str = ""  # shown in WhatsApp for BANK_TRANSFER customers
    twilio_admin_tap_sid: str = ""
    twilio_admin_bank_sid: str = ""
    twilio_admin_missing_phone_sid: str = ""
    twilio_admin_phone: str = ""
    qbo_bank_payment_method_id: str | None = None

    @property
    def api_base_url(self) -> str:
        return API_BASE_URLS[self.environment]


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def get_settings() -> Settings:
    environment = os.getenv("QBO_ENVIRONMENT", "production").strip().lower()
    if environment not in VALID_ENVIRONMENTS:
        raise ConfigError(
            f"QBO_ENVIRONMENT must be one of {sorted(VALID_ENVIRONMENTS)}, "
            f"got '{environment}'."
        )
    return Settings(
        client_id=_require("QBO_CLIENT_ID"),
        client_secret=_require("QBO_CLIENT_SECRET"),
        redirect_uri=(
            os.getenv("QBO_REDIRECT_URI", "").strip()
            or "https://prime-qbo-webhook.netlify.app/oauth/callback"
        ),
        environment=environment,
        minor_version=os.getenv("QBO_MINOR_VERSION", "75").strip(),
        # QBO tokens refresh on every use (Intuit rotates them immediately).
        # On Render's ephemeral filesystem, rotated tokens are lost on redeploy.
        # To persist tokens across redeploys, mount a persistent disk in Render
        # and set QBO_TOKEN_PATH to an absolute path on that disk.
        # Local dev: relative path "tokens.json" is fine.
        # Render: set QBO_TOKEN_PATH=/data/tokens.json (adjust mount point as needed).
        token_path=os.getenv("QBO_TOKEN_PATH", "tokens.json").strip(),
        realm_id=(os.getenv("QBO_REALM_ID", "").strip() or None),
        webhook_verifier_token=(os.getenv("QBO_WEBHOOK_VERIFIER_TOKEN", "").strip() or None),
        tap_secret_key=(os.getenv("TAP_SECRET_KEY", "").strip() or None),
        tap_redirect_url=os.getenv("TAP_REDIRECT_URL", "http://localhost:8080/payment/success").strip(),
        tap_webhook_url=os.getenv("TAP_WEBHOOK_URL", "http://localhost:8000/webhook/tap").strip(),
        # Same pattern as token_path: relative path for local dev, absolute path on
        # Render persistent disk to survive redeploys.
        # Local dev: "hub.db" is fine.
        # Render: set DATABASE_PATH=/data/hub.db (adjust mount point as needed).
        database_path=os.getenv("DATABASE_PATH", "hub.db").strip(),
        twilio_account_sid=(os.getenv("TWILIO_ACCOUNT_SID", "").strip() or None),
        twilio_auth_token=(os.getenv("TWILIO_AUTH_TOKEN", "").strip() or None),
        twilio_whatsapp_from=os.getenv("TWILIO_WHATSAPP_FROM", "").strip(),
        twilio_content_sid=os.getenv("TWILIO_CONTENT_SID", "").strip(),
        twilio_confirmation_content_sid=os.getenv("TWILIO_CONFIRMATION_CONTENT_SID", "").strip(),
        qbo_deposit_account_id=(os.getenv("QBO_DEPOSIT_ACCOUNT_ID", "").strip() or None),
        bank_transfer_info=os.getenv("BANK_TRANSFER_INFO", "").strip(),
        twilio_admin_tap_sid=os.getenv("TWILIO_ADMIN_TAP_SID", "").strip(),
        twilio_admin_bank_sid=os.getenv("TWILIO_ADMIN_BANK_SID", "").strip(),
        twilio_admin_missing_phone_sid=os.getenv(
            "TWILIO_ADMIN_MISSING_PHONE_CONTENT_SID", ""
        ).strip(),
        twilio_admin_phone=os.getenv("TWILIO_ADMIN_PHONE", "").strip(),
        qbo_bank_payment_method_id=(
            os.getenv("QBO_BANK_PAYMENT_METHOD_ID", "").strip() or None
        ),
    )
