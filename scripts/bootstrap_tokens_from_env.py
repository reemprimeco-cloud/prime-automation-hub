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

    try:
        from auth.token_store import decode_qbo_tokens_env

        decode_qbo_tokens_env()
    except ValueError as exc:
        print(f"\n✗  {exc}\n", file=sys.stderr)
        print(
            "Fix on Mac:\n"
            "  python -m scripts.import_tokens\n"
            "Then paste tokens_for_render.b64 (or tokens.json) into Render → QBO_TOKENS_JSON",
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
