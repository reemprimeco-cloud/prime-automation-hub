"""Load QBO tokens on Render and verify the API works.

Run in Render Shell after updating QBO_TOKENS_JSON in the dashboard:

    python -m scripts.setup_render
    python -m scripts.setup_render 2555

Do NOT run repair_automation here — that script is for your Mac only.
"""
from __future__ import annotations

import sys

from auth.token_store import bootstrap_from_env
from config import get_settings


def _fail(msg: str) -> None:
    print(f"\n✗  {msg}", file=sys.stderr)
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"✓  {msg}")


def main() -> None:
    print("\nRender token setup\n")

    if not __import__("os").getenv("QBO_TOKENS_JSON", "").strip():
        _fail(
            "QBO_TOKENS_JSON is not set on Render.\n"
            "On your Mac: python -m scripts.repair_automation\n"
            "Then paste tokens_for_render.b64 into Render → Environment → QBO_TOKENS_JSON"
        )

    for name in ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET"):
        if not __import__("os").getenv(name, "").strip():
            _fail(f"{name} is not set on Render → Environment.")

    settings = get_settings()
    _ok(f"QBO_CLIENT_ID: {settings.client_id[:12]}...")
    _ok(f"QBO_ENVIRONMENT: {settings.environment}")

    if bootstrap_from_env(settings.token_path, force=True):
        _ok(f"tokens.json loaded from QBO_TOKENS_JSON → {settings.token_path}")
    else:
        _fail(
            "bootstrap failed — QBO_TOKENS_JSON must be base64 from "
            "`python -m scripts.encode_tokens_for_render` on your Mac (not raw JSON)."
        )

    from qbo.client import QuickBooksClient

    try:
        qbo = QuickBooksClient(settings=settings)
        info = qbo.get_company_info()
        name = info.get("CompanyInfo", {}).get("CompanyName", info)
        _ok(f"QuickBooks API OK — {name}")
    except Exception as exc:
        _fail(f"QuickBooks API failed: {exc}")

    if len(sys.argv) == 2:
        doc = sys.argv[1].strip().lstrip("#")
        print(f"\nProcessing invoice #{doc}...")
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "scripts.process_invoice", doc],
        )
        raise SystemExit(result.returncode)

    print("\nDone. Test: curl https://prime-automation-hub.onrender.com/health\n")


if __name__ == "__main__":
    main()
