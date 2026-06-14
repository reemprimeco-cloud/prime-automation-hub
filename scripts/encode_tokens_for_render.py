"""Print base64-encoded tokens.json for Render QBO_TOKENS_JSON env var.

Usage:
    python -m scripts.encode_tokens_for_render

Copy the output into Render → Environment → QBO_TOKENS_JSON, then redeploy.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from config import get_settings


def main() -> None:
    settings = get_settings()
    path = Path(settings.token_path)
    if not path.is_file():
        abs_path = path.resolve()
        print(
            f"Missing {abs_path}. Run from the project root:\n"
            "  cd ~/Downloads/quickbooks-integration\n"
            "  source .venv/bin/activate\n"
            "  python -m scripts.authorize",
            file=sys.stderr,
        )
        sys.exit(1)

    raw = path.read_text(encoding="utf-8")
    json.loads(raw)  # validate
    encoded = base64.b64encode(raw.encode()).decode().rstrip("=")
    print(encoded)
    print(
        "\nPaste the line above into Render → Environment → QBO_TOKENS_JSON "
        "(base64 only — no quotes, no raw JSON). Then Manual Deploy.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
