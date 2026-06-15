"""Print Intuit OAuth setup checklist and verify local config."""
from __future__ import annotations

import urllib.error
import urllib.request

from config import get_settings

# Prime Automation Hub — known key mismatch from Intuit portal (Development ≠ Production)
_DEV_CLIENT_ID = "ABLdbtne5RXprayvcyJfyt13Lz0IZ5BR1qkrMVMLQcCtwGwnW9"
_PROD_CLIENT_ID = "ABMb73CANOBxJGFcAy61zmfo4lswB4ryEhb4s1yb9ml2cpXjMm"


def main() -> None:
    settings = get_settings()
    print("\nIntuit OAuth diagnostic\n")
    print(f"  QBO_ENVIRONMENT     {settings.environment}")
    print(f"  QBO_CLIENT_ID       {settings.client_id}")
    print(f"  QBO_REDIRECT_URI    {settings.redirect_uri}")
    print(f"  QBO_REALM_ID        {settings.realm_id or '(not set)'}")

    if settings.environment == "production" and settings.client_id == _DEV_CLIENT_ID:
        print(
            "\n  ✗  WRONG KEYS: You are using the DEVELOPMENT Client ID with production.\n"
            "     Intuit → Keys & credentials → Production tab → copy Client ID + Secret.\n"
            f"     Production Client ID should be: {_PROD_CLIENT_ID}\n"
        )
    elif settings.environment == "production" and settings.client_id == _PROD_CLIENT_ID:
        print("  ✓  Production Client ID looks correct")

    if settings.client_secret.startswith("PASTE_") or not settings.client_secret:
        print("  ✗  QBO_CLIENT_SECRET not set — copy from Production tab in Intuit portal")

    try:
        with urllib.request.urlopen(settings.redirect_uri.split("?", 1)[0], timeout=15):
            print("  ✓  Redirect URI reachable")
    except urllib.error.HTTPError as exc:
        print(f"  ✗  Redirect URI HTTP {exc.code}")

    print(
        "\nRender must also use Production keys + Production webhook verifier:\n"
        "  QBO_WEBHOOK_VERIFIER_TOKEN=a112e073-d767-4ee2-a6ac-385585fbc12d\n"
        "  (NOT d839fdc2-... which is Development)\n"
    )


if __name__ == "__main__":
    main()
