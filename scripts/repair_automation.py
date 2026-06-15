"""One-command repair: validate setup → authorize → encode for Render.

    cd ~/Documents/prime-automation-hub
    source .venv/bin/activate
    python -m scripts.repair_automation

Requires one manual step: after the browser opens, click Connect to QuickBooks,
then paste the success callback URL when prompted.
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _step(msg: str) -> None:
    print(f"\n{'=' * 60}\n{msg}\n{'=' * 60}")


def _fail(msg: str) -> None:
    print(f"\n✗  {msg}", file=sys.stderr)
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"✓  {msg}")


def _check_env() -> None:
    _step("1/4 — Checking configuration")
    env_path = ROOT / ".env"
    if not env_path.is_file() and not os.getenv("QBO_CLIENT_ID", "").strip():
        _fail(f"Missing {env_path}. Copy .env.example and fill in Production keys from Intuit.")

    from config import get_settings

    settings = get_settings()
    if settings.environment != "production":
        _fail("QBO_ENVIRONMENT must be production for live QuickBooks.")

    redirect = settings.redirect_uri.strip()
    if not redirect:
        _fail("QBO_REDIRECT_URI is empty.")

    try:
        with urllib.request.urlopen(redirect.split("?", 1)[0], timeout=15) as resp:
            if resp.status >= 400:
                _fail(f"Redirect URI not reachable: {redirect} (HTTP {resp.status})")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _fail(
                f"Redirect URI returns 404: {redirect}\n"
                "Use https://prime-qbo-webhook.netlify.app/oauth/callback"
            )
        if exc.code >= 400:
            _fail(f"Redirect URI error: {redirect} (HTTP {exc.code})")
    except OSError as exc:
        _fail(f"Cannot reach redirect URI {redirect}: {exc}")

    _ok(f"Environment: {settings.environment}")
    _ok(f"Client ID: {settings.client_id[:12]}...")
    _ok(f"Redirect URI: {redirect}")
    _ok("Use Intuit Developer → Production tab keys (not Development/sandbox).")


def _clear_bad_tokens() -> None:
    _step("2/4 — Clearing stale tokens.json")
    token_path = ROOT / "tokens.json"
    if token_path.is_file():
        token_path.unlink()
        _ok("Removed old tokens.json")
    else:
        _ok("No tokens.json to remove")


def _authorize() -> None:
    _step("3/4 — QuickBooks authorize (one browser step)")
    print(
        "Browser will open. Sign in → pick your LIVE company → Connect.\n"
        "You must land on the Netlify success page (not an Intuit error page).\n"
        "Copy the full URL from the address bar and paste it here.\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "scripts.authorize"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        _fail("authorize failed — fix Intuit Production redirect URI and keys, then retry.")


def _encode_and_verify() -> None:
    _step("4/4 — Verify + encode for Render")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.get_company_info"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        _fail("get_company_info failed after authorize.")

    _ok("QuickBooks API connection works on this Mac")

    encoded = subprocess.run(
        [sys.executable, "-m", "scripts.encode_tokens_for_render"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if encoded.returncode != 0:
        print(encoded.stderr, file=sys.stderr)
        _fail("encode_tokens_for_render failed")

    out_path = ROOT / "tokens_for_render.b64"
    b64 = encoded.stdout.strip().splitlines()[0]
    out_path.write_text(b64 + "\n", encoding="utf-8")
    _ok(f"Saved base64 to {out_path}")

    print(
        "\n"
        "── Render (paste once) ──────────────────────────────────────\n"
        "1. Render → prime-automation-hub → Environment\n"
        "2. Set QBO_TOKENS_JSON to the contents of tokens_for_render.b64\n"
        "3. Confirm QBO_CLIENT_ID / QBO_CLIENT_SECRET match this Mac .env\n"
        "4. Manual Deploy\n"
        "5. Render Shell:\n"
        "     python -m scripts.bootstrap_tokens_from_env\n"
        "     python -m scripts.process_invoice 2555\n"
        "\n"
        "Or test: python -m scripts.check_automation\n"
    )


def main() -> None:
    os.chdir(ROOT)
    if os.getenv("RENDER"):
        _fail(
            "repair_automation runs on your Mac, not Render Shell.\n\n"
            "On Render, after updating QBO_TOKENS_JSON:\n"
            "  python -m scripts.setup_render\n"
            "  python -m scripts.setup_render 2555\n\n"
            "On your Mac (authorize + create tokens):\n"
            "  source .venv/bin/activate\n"
            "  python -m scripts.repair_automation"
        )
    _check_env()
    _clear_bad_tokens()
    _authorize()
    _encode_and_verify()


if __name__ == "__main__":
    main()
