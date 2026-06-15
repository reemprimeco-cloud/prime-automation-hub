"""Import QBO tokens from Intuit OAuth Playground (when browser authorize fails).

Use when authorize shows "no sandbox companies" but you can get tokens in the
Intuit OAuth 2.0 Playground with your Production keys:

  https://developer.intuit.com/app/developer/playground

In the playground:
  1. Select your app → Production Client ID + Secret (must match .env)
  2. Redirect URI: OAuth2Playground OR Netlify callback (registered on Production tab)
  3. Scope: com.intuit.quickbooks.accounting
  4. Get authorization code → exchange for tokens
  5. Copy accessToken, refreshToken, and realmId

Then run:

    python -m scripts.import_tokens

Or non-interactive:

    python -m scripts.import_tokens --access-token '...' --refresh-token '...' --realm-id 9130357945907536
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from auth.token_store import TokenData, save_tokens
from config import get_settings


def _prompt(label: str) -> str:
    value = input(f"{label}: ").strip()
    if not value:
        print(f"Missing {label}", file=sys.stderr)
        raise SystemExit(1)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Import QBO tokens from OAuth Playground")
    parser.add_argument("--access-token", help="accessToken from playground")
    parser.add_argument("--refresh-token", help="refreshToken from playground")
    parser.add_argument("--realm-id", help="realmId / company ID")
    parser.add_argument("--expires-in", type=int, default=3600, help="access token TTL seconds")
    parser.add_argument(
        "--refresh-expires-in",
        type=int,
        default=8726400,
        help="refresh token TTL seconds (~101 days)",
    )
    args = parser.parse_args()

    settings = get_settings()
    access = args.access_token or _prompt("accessToken")
    refresh = args.refresh_token or _prompt("refreshToken")
    realm = args.realm_id or _prompt("realmId")

    now = time.time()
    save_tokens(
        settings.token_path,
        TokenData(
            access_token=access,
            refresh_token=refresh,
            realm_id=str(realm),
            access_token_expires_at=now + args.expires_in,
            refresh_token_expires_at=now + args.refresh_expires_in,
        ),
    )
    print(f"\n✓  Saved {settings.token_path}")

    print("\nVerifying QuickBooks API...")
    from qbo.client import QuickBooksClient

    try:
        qbo = QuickBooksClient(settings=settings)
        info = qbo.get_company_info()
        name = info.get("CompanyInfo", {}).get("CompanyName", info)
        print(f"✓  Connected: {name} (realmId {realm})")
    except Exception as exc:
        print(f"✗  API check failed: {exc}", file=sys.stderr)
        err = str(exc).lower()
        if "invalid_client" in err:
            print(
                "\ninvalid_client — Client ID + Secret are wrong or don't match.\n"
                "Intuit → Keys & credentials → Production → Show credentials\n"
                "Copy BOTH using the copy button (don't retype).\n"
                f"  Client ID in .env: {settings.client_id}\n"
                "  Update QBO_CLIENT_SECRET in .env to match the SAME tab.",
                file=sys.stderr,
            )
        elif "invalid_grant" in err:
            print(
                "\ninvalid_grant — refresh token doesn't match this Client ID.\n"
                "Get a NEW refreshToken from OAuth Playground using the SAME\n"
                "Client ID + Secret as your .env, then run import_tokens again.",
                file=sys.stderr,
            )
        else:
            print(
                f"\nConfirm .env matches Intuit Production tab exactly.\n"
                f"  Client ID: {settings.client_id[:20]}...",
                file=sys.stderr,
            )
        raise SystemExit(1)

    import subprocess

    encoded = subprocess.run(
        [sys.executable, "-m", "scripts.encode_tokens_for_render"],
        capture_output=True,
        text=True,
    )
    if encoded.returncode == 0:
        b64 = encoded.stdout.strip().splitlines()[0]
        out = Path(settings.token_path).with_name("tokens_for_render.b64")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(b64 + "\n")
        print(f"✓  Render base64 → {out}")
        print("\nNext: paste into Render → QBO_TOKENS_JSON → Manual Deploy")
        print("      python -m scripts.bootstrap_tokens_from_env  (on Render Shell)")


if __name__ == "__main__":
    main()
