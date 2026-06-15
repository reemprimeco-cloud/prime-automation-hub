"""Force reload tokens.json from QBO_TOKENS_JSON.

Run on Render after updating QBO_TOKENS_JSON (no redeploy needed):

    python -m scripts.bootstrap_tokens_from_env
    python -m scripts.get_company_info
"""
from __future__ import annotations

import sys

from auth.token_store import bootstrap_from_env
from config import get_settings


def main() -> None:
    import base64
    import json
    import os

    settings = get_settings()
    env_val = os.getenv("QBO_TOKENS_JSON", "").strip()

    if not env_val:
        print(
            "QBO_TOKENS_JSON is empty on Render.\n\n"
            "On your Mac:\n"
            "  cd ~/Documents/prime-automation-hub\n"
            "  source .venv/bin/activate\n"
            "  python -m scripts.repair_automation\n\n"
            "Then paste tokens_for_render.b64 into Render → Environment → QBO_TOKENS_JSON",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if env_val.startswith("{"):
        print(
            "QBO_TOKENS_JSON contains raw JSON — Render needs base64 instead.\n\n"
            "On your Mac run: python -m scripts.encode_tokens_for_render\n"
            "Paste the single base64 line (starts with eyJ...), not the JSON object.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        padded = env_val + "=" * (4 - len(env_val) % 4) if len(env_val) % 4 != 0 else env_val
        base64.b64decode(padded).decode("utf-8")
    except Exception as exc:
        print(
            f"QBO_TOKENS_JSON is not valid base64: {exc}\n\n"
            "Re-run on Mac: python -m scripts.repair_automation\n"
            "Copy the entire tokens_for_render.b64 file with no extra spaces or quotes.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if bootstrap_from_env(settings.token_path, force=True):
        print(f"Tokens loaded from QBO_TOKENS_JSON → {settings.token_path}")
        raise SystemExit(0)

    print(
        "Bootstrap failed — JSON inside QBO_TOKENS_JSON may be invalid.\n"
        "Create fresh tokens on your Mac: python -m scripts.repair_automation",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
