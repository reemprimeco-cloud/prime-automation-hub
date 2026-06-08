"""OAuth 2.0 helpers built on Intuit's official AuthClient.

NOTE: Constructing an AuthClient performs a network call to fetch Intuit's OpenID
discovery document, so these functions must run in an environment that can reach
Intuit's endpoints (i.e. your machine / server — not an isolated sandbox).
"""
from __future__ import annotations

import time

from intuitlib.client import AuthClient

from config import Settings
from auth.token_store import TokenData


def build_auth_client(
    settings: Settings,
    refresh_token: str | None = None,
    realm_id: str | None = None,
) -> AuthClient:
    return AuthClient(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=settings.redirect_uri,
        environment=settings.environment,
        refresh_token=refresh_token,
        realm_id=realm_id,
    )


def token_data_from_client(client: AuthClient, realm_id: str) -> TokenData:
    """Snapshot the tokens currently held by an AuthClient into a TokenData."""
    now = time.time()
    # Sensible fallbacks: access ~1h, refresh ~100 days.
    expires_in = float(client.expires_in or 3600)
    refresh_expires_in = float(client.x_refresh_token_expires_in or 8_640_000)
    return TokenData(
        access_token=client.access_token,
        refresh_token=client.refresh_token,
        realm_id=str(realm_id),
        access_token_expires_at=now + expires_in,
        refresh_token_expires_at=now + refresh_expires_in,
    )


def refresh_tokens(settings: Settings, tokens: TokenData) -> TokenData:
    """Exchange the refresh token for a fresh access token.

    QuickBooks rotates refresh tokens, so the returned TokenData may contain a
    NEW refresh token — callers must persist the result.
    """
    client = build_auth_client(
        settings,
        refresh_token=tokens.refresh_token,
        realm_id=tokens.realm_id,
    )
    client.refresh(refresh_token=tokens.refresh_token)
    return token_data_from_client(client, tokens.realm_id)
