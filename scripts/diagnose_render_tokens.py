"""Diagnose QBO_TOKENS_JSON on Render Shell.

    python -m scripts.diagnose_render_tokens
"""
from __future__ import annotations

import json
import os
import sys


def main() -> None:
    print("\nQBO_TOKENS_JSON diagnostic\n")
    raw = os.getenv("QBO_TOKENS_JSON", "")
    val = raw.strip()

    if not val:
        print("✗  QBO_TOKENS_JSON is EMPTY")
        print("\nFix on Mac: python -m scripts.import_tokens")
        print("Then paste tokens_for_render.b64 → Render → Environment")
        raise SystemExit(1)

    print(f"  Length: {len(val)} chars")
    print(f"  Format: {'raw JSON' if val.startswith('{') else 'base64'}")

    try:
        from auth.token_store import decode_qbo_tokens_env

        decoded = decode_qbo_tokens_env()
        data = json.loads(decoded)
        print(f"  realm_id: {data.get('realm_id')}")
        print(f"  access_token: {str(data.get('access_token', ''))[:20]}...")
        print(f"  refresh_token: {str(data.get('refresh_token', ''))[:20]}...")
        print("\n✓  QBO_TOKENS_JSON is valid — run: python -m scripts.bootstrap_tokens_from_env")
    except Exception as exc:
        print(f"\n✗  Invalid: {exc}")
        print("\nFix on Mac: python -m scripts.import_tokens → update Render → redeploy")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
