"""One-time OAuth 2.0 authorization.

Opens your browser to Intuit's consent screen, captures the redirect on a local
server, exchanges the authorization code for tokens, and saves them to disk.

Run from the project root:
    python -m scripts.authorize
"""
from __future__ import annotations

import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from intuitlib.enums import Scopes

from config import get_settings
from auth.oauth import build_auth_client, token_data_from_client
from auth.token_store import save_tokens

_result: dict[str, str | None] = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    expected_path = "/callback"

    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        _result["code"] = qs.get("code", [None])[0]
        _result["realm_id"] = qs.get("realmId", [None])[0]
        _result["state"] = qs.get("state", [None])[0]
        _result["error"] = qs.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = (
            "Authorization complete. You can close this tab and return to the terminal."
            if not _result.get("error")
            else f"Authorization failed: {_result['error']}. Check the terminal."
        )
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *args):  # silence default request logging
        return


def _parse_callback_url(url: str) -> dict[str, str | None]:
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query)
    return {
        "code": qs.get("code", [None])[0],
        "realm_id": qs.get("realmId", [None])[0],
        "state": qs.get("state", [None])[0],
        "error": qs.get("error", [None])[0],
    }


def _exchange_and_save(auth_client, settings, code: str, realm_id: str) -> None:
    print("Exchanging authorization code for tokens...")
    auth_client.get_bearer_token(code, realm_id=realm_id)
    tokens = token_data_from_client(auth_client, realm_id)
    save_tokens(settings.token_path, tokens)
    print(f"\nSuccess! Tokens saved to {settings.token_path}")
    print(f"Connected company (realmId): {realm_id}")
    print("You can now run the retrieval scripts, e.g. `python -m scripts.list_invoices`.")


def main() -> None:
    settings = get_settings()
    redirect = urlparse(settings.redirect_uri)
    host = redirect.hostname or "localhost"
    port = redirect.port or 8000
    _CallbackHandler.expected_path = redirect.path or "/callback"
    use_local_server = host in {"localhost", "127.0.0.1"}

    auth_client = build_auth_client(settings)
    auth_url = auth_client.get_authorization_url([Scopes.ACCOUNTING])
    expected_state = auth_client.state_token

    print("Opening your browser to authorize access to QuickBooks...")
    print(f"If it doesn't open automatically, paste this URL:\n\n{auth_url}\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    if use_local_server:
        server = HTTPServer((host, port), _CallbackHandler)
        print(f"Listening for the redirect on {settings.redirect_uri} ...")
        while "code" not in _result and "error" not in _result:
            server.handle_request()
        callback = _result
    else:
        print(
            f"\nAfter you authorize in the browser, copy the full callback URL "
            f"from the address bar ({settings.redirect_uri}?...) and paste it here:\n"
        )
        pasted = input("Callback URL: ").strip()
        if "oauth2/error" in pasted or "error=" in parse_qs(urlparse(pasted).query):
            print(
                "\nThat URL is an Intuit error page, not a successful callback.\n"
                "Fix the redirect URI in Intuit Developer → Production → Redirect URIs,\n"
                f"then try again. It must match exactly:\n\n  {settings.redirect_uri}\n",
                file=sys.stderr,
            )
        callback = _parse_callback_url(pasted)

    if callback.get("error"):
        print(f"Authorization failed: {callback['error']}", file=sys.stderr)
        sys.exit(1)
    if callback.get("state") != expected_state:
        print("State token mismatch — possible CSRF. Aborting.", file=sys.stderr)
        sys.exit(1)

    code = callback.get("code")
    realm_id = callback.get("realm_id")
    if not code:
        print("No authorization code in callback URL.", file=sys.stderr)
        sys.exit(1)
    if not realm_id:
        print("No realmId returned — did you select a company?", file=sys.stderr)
        sys.exit(1)

    _exchange_and_save(auth_client, settings, code, realm_id)


if __name__ == "__main__":
    main()
