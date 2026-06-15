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
    settings = get_settings()
    if bootstrap_from_env(settings.token_path, force=True):
        print(f"Tokens loaded from QBO_TOKENS_JSON → {settings.token_path}")
        raise SystemExit(0)
    print(
        "Failed — set QBO_TOKENS_JSON on Render (base64 from encode_tokens_for_render).",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
