# QuickBooks Online API Integration (Python)

Connect to the QuickBooks Online (QBO) API using OAuth 2.0 and retrieve **Company
Info**, **Customers**, and **Invoices**. Credentials live entirely in environment
variables; tokens are cached on disk and refreshed automatically.

> **Scope note:** This project covers the QuickBooks integration only. Tap and
> WhatsApp work is intentionally **not** included yet.

---

## How it works (the short version)

1. You register an app on the Intuit Developer portal and get a **Client ID** and
   **Client Secret**.
2. You run `python -m scripts.authorize` once. It opens your browser, you log into
   QuickBooks and pick a company, and Intuit redirects back to a tiny local server
   that captures the result.
3. The script exchanges the authorization code for an **access token**, a
   **refresh token**, and your company's **realmId**, then saves them to
   `tokens.json`.
4. Every other script uses those tokens. When the access token expires (~1 hour),
   the client refreshes it automatically using the refresh token and re-saves the
   file. Refresh tokens last ~100 days.

---

## Project structure

```
quickbooks-integration/
├── README.md
├── requirements.txt
├── .env.example            # template — copy to .env and fill in
├── .gitignore              # keeps .env and tokens.json out of git
├── config.py               # loads & validates settings from env vars
├── auth/
│   ├── token_store.py      # save/load tokens (JSON, 0600 perms, expiry tracking)
│   └── oauth.py            # Intuit AuthClient wrapper + refresh logic
├── qbo/
│   └── client.py           # REST client: auto-refresh + retrieval helpers
├── scripts/
│   ├── authorize.py        # one-time OAuth flow (run this first)
│   ├── get_company_info.py
│   ├── get_customers.py
│   └── list_invoices.py    # TEST SCRIPT: lists the latest 10 invoices
└── tests/
    └── test_client.py      # offline unit tests (no live creds needed)
```

---

## Prerequisites

- **Python 3.9+**
- An **Intuit Developer account**: <https://developer.intuit.com>
- A QuickBooks Online company you can authorize against (for `production`, a real
  company; Intuit also provides sandbox companies for testing).

---

## Step 1 — Create / configure your Intuit app

1. Go to <https://developer.intuit.com> → **My Apps** → create an app (or open an
   existing one). Select the **com.intuit.quickbooks.accounting** scope.
2. Open **Keys & credentials**. For production, use the **Production** keys
   (a published/approved app is required for production keys); for testing, use
   the **Development/Sandbox** keys. Copy the **Client ID** and **Client Secret**.
3. Under **Redirect URIs**, add exactly:
   ```
   http://localhost:8000/callback
   ```
   This must match `QBO_REDIRECT_URI` in your `.env` **character-for-character**.

---

## Step 2 — Install

```bash
cd quickbooks-integration
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Step 3 — Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

| Variable            | Required | Description                                                        |
|---------------------|----------|--------------------------------------------------------------------|
| `QBO_CLIENT_ID`     | ✅       | From your Intuit app's Keys & credentials.                         |
| `QBO_CLIENT_SECRET` | ✅       | From your Intuit app's Keys & credentials.                         |
| `QBO_REDIRECT_URI`  | ✅       | Must match a Redirect URI on the app. Default `http://localhost:8000/callback`. |
| `QBO_ENVIRONMENT`   | ✅       | `production` or `sandbox`.                                         |
| `QBO_MINOR_VERSION` | ⬜       | API minor version (default `75`). See note below.                  |
| `QBO_TOKEN_PATH`    | ⬜       | Where tokens are cached (default `tokens.json`).                   |
| `QBO_REALM_ID`      | ⬜       | Force a specific company; normally auto-detected during authorize. |

All credentials are read from the environment — nothing is hard-coded.

---

## Step 4 — Authorize (one time)

```bash
python -m scripts.authorize
```

- Your browser opens Intuit's consent screen.
- Log in, choose the company, and approve.
- You're redirected to `http://localhost:8000/callback`; the script captures the
  code + realmId, exchanges them for tokens, and writes `tokens.json`.

You should see `Success! Tokens saved to tokens.json` and your company's realmId.

> Run all scripts from the **project root** using `python -m scripts.<name>` so the
> package imports resolve correctly.

---

## Step 5 — Retrieve data

```bash
# Company Info
python -m scripts.get_company_info

# Customers
python -m scripts.get_customers

# Latest 10 invoices  ← the success-criteria test script
python -m scripts.list_invoices
```

Example output of `list_invoices`:

```
Latest 10 invoice(s):

Doc #      Date         Customer                            Total      Balance
---------------------------------------------------------------------------------
1037       2026-05-30   Sunshine Bakery                  1,250.00       0.00
1036       2026-05-28   Harbor Logistics                   480.00     480.00
...
```

### Use the client in your own code

```python
from qbo.client import QuickBooksClient

client = QuickBooksClient()
info      = client.get_company_info()
customers = client.get_customers(max_results=100)
invoices  = client.get_invoices(max_results=10)        # latest by TxnDate
rows      = client.query("SELECT * FROM Invoice WHERE Balance > '0'")
```

---

## Running the offline tests

These mock the HTTP layer, so they need **no** credentials:

```bash
python -m pytest -q
```

---

## Token lifecycle & refresh

- **Access token** — valid ~1 hour. The client checks expiry before each call and
  refreshes proactively; it also refreshes once and retries on a `401`.
- **Refresh token** — valid ~100 days and **rotates** on each refresh, so the new
  value is always written back to `tokens.json`.
- If the refresh token expires (or is revoked), you'll get a clear
  `NotAuthorizedError` telling you to re-run `scripts.authorize`.

---

## Switching environments

Set `QBO_ENVIRONMENT` in `.env`:

- `production` → `https://quickbooks.api.intuit.com`
- `sandbox`    → `https://sandbox-quickbooks.api.intuit.com`

Production and sandbox use **different** client keys and have separate tokens —
after switching, delete `tokens.json` (or point `QBO_TOKEN_PATH` elsewhere) and
re-authorize.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `redirect_uri did not match` on the consent screen | `QBO_REDIRECT_URI` doesn't exactly match a Redirect URI registered on the Intuit app (scheme, host, port, path, trailing slash all matter). |
| Browser opens but terminal never continues | Something else is using the port, or the redirect host/port differs from what the script binds. Free the port or change `QBO_REDIRECT_URI`. |
| `401 Unauthorized` repeatedly | Tokens stale/revoked — delete `tokens.json` and re-run `scripts.authorize`. |
| `403 Forbidden` / `ApplicationAuthorizationFailed` | Using production keys against a sandbox company (or vice versa), or the app lacks the accounting scope. |
| `invalid_grant` on refresh | Refresh token expired (>100 days) or already rotated elsewhere. Re-authorize. |
| `Host not in allowlist` | You're running in a restricted/sandboxed network that can't reach Intuit. Run on a machine with normal internet access. |
| Empty results | The connected company genuinely has no customers/invoices, or you're pointed at the wrong company (`realmId`). |

---

## Security notes

- **Never commit** `.env` or `tokens.json` — both are in `.gitignore`.
- `tokens.json` is written with `0600` (owner-only) permissions.
- Treat the Client Secret and refresh token like passwords. For servers, inject
  these via your platform's secrets manager rather than a file on disk.

---

## Not in scope (yet)

Tap and WhatsApp integrations are deliberately excluded from this project per the
current objective.
