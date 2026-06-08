# Phase 3 — Tap Payment Link Generation

## What this phase does

When a new QuickBooks invoice is created, the system:

1. Fetches the full invoice and customer from QuickBooks.
2. Validates the invoice is open and unpaid.
3. Creates a hosted payment link via the Tap Payments API.
4. Saves the link to the local database (`hub.db`).
5. Writes the payment link into the invoice's **PrivateNote** in QuickBooks.

No payment is captured in this phase. No WhatsApp messages are sent.

---

## New environment variables

Add these to your `.env` (see `.env.example`):

| Variable | Required | Description |
|---|---|---|
| `TAP_SECRET_KEY` | ✅ | From Tap Dashboard → Developers → API Credentials. Use `sk_live_…` for production. |
| `TAP_REDIRECT_URL` | ✅ | Customer lands here after paying. Example: `https://prime-automation-hub.onrender.com/payment/success` |
| `TAP_WEBHOOK_URL` | ✅ | Tap POSTs the result here. Example: `https://hub.primekw.com/webhook/tap` |
| `DATABASE_PATH` | ⬜ | Path to the SQLite database file (default: `hub.db`) |

> **Production note:** `TAP_REDIRECT_URL` and `TAP_WEBHOOK_URL` must be publicly accessible HTTPS URLs. For local testing, use ngrok:
> ```bash
> ngrok http 8080
> # then set TAP_REDIRECT_URL=https://<ngrok-id>.ngrok.io/payment/success
> ```

---

## Database migration

The `payment_links` table is created automatically on first run. For reference, the full SQL is in:

```
db/migrations/001_create_payment_links.sql
```

For PostgreSQL, apply it manually:
```bash
psql $DATABASE_URL -f db/migrations/001_create_payment_links.sql
```

Then swap `db/connection.py` to use `psycopg2` with `DATABASE_URL`.

---

## New packages

None — Phase 3 uses only `requests` (already installed).

---

## Test script

Run against a real QuickBooks invoice with real Tap credentials:

```bash
# List your open invoices to pick an ID
python -m scripts.test_invoice_to_tap --list

# Run on the latest open invoice (auto-selected)
python -m scripts.test_invoice_to_tap

# Run on a specific invoice ID
python -m scripts.test_invoice_to_tap 42

# Dry run — see what would be sent to Tap, no API call
python -m scripts.test_invoice_to_tap 42 --dry-run
```

### Expected output (success)

```
══════════════════════════════════════════════════════════════════
  Phase 3 Test — Invoice → Tap Payment Link
══════════════════════════════════════════════════════════════════

  [1/4] Fetching invoice...  → done
    Invoice   #1089  |  ID: 42
    Customer  Al-Rashid Trading Co
    Amount    250.000 KWD  (balance: 250.000 KWD)

  [2/4] Fetching customer...  → Ahmed Al-Rashid  |  +96565068000

  [3/4] Running workflow...  → done

══════════════════════════════════════════════════════════════════
  ✓  Status                       New link generated
  ✓  Invoice ID                   42
  ✓  Invoice Number               #1089
  ✓  Customer                     Ahmed Al-Rashid
  ✓  Amount                       250.000 KWD
  ✓  Tap Charge ID                chg_TS07A5…
  ✓  QBO note updated             YES

  Payment URL:
    https://checkout.tap.company/v2/…

──────────────────────────────────────────────────────────────────
  [4/4] Idempotency check (re-run)...  → PASS — same link returned, no duplicate created
══════════════════════════════════════════════════════════════════
```

The fourth step proves idempotency: running the script twice on the same invoice returns the existing link without calling Tap again.

---

## Offline unit tests

No credentials needed:

```bash
python -m pytest tests/test_tap_client.py tests/test_invoice_to_tap_workflow.py -v
```

These cover: request building, retry logic, auth errors, idempotency, invoice validation, QBO update resilience, and the landline-exclusion rule for Tap's phone field.

---

## How payment link data is stored in QuickBooks

The link is written to the invoice's **PrivateNote** field (visible in QuickBooks but not on the printed invoice). Format:

```
--- Prime Automation Hub ---
Payment Link:
https://checkout.tap.company/v2/session/chg_TS07…

Tap Reference:
chg_TS07A5020231643Obe10906052

Generated At:
2026-06-06T12:00:00+00:00
```

Any existing PrivateNote content is preserved above the new block.

---

## Important notes

- **Payment link session expiry:** Tap's hosted checkout page expires after 60 minutes (the maximum allowed). The Tap charge reference (`chg_…`) remains valid. Phase 4 will implement link regeneration when needed.
- **QBO update resilience:** If the QuickBooks API is unavailable when writing the PrivateNote, the payment link is still saved to the database and is fully functional. The `qbo_note_updated` flag in `payment_links` will be `0` — re-running the script will not create a duplicate but will retry the QBO write.
- **Idempotency:** One payment link per invoice, enforced by a `UNIQUE` constraint on `payment_links.invoice_id`. Running the workflow twice on the same invoice returns the existing link.

---

## What comes next

- **Phase 4** — Payment capture: handle Tap's `charge.captured` webhook, mark the QBO invoice paid, and trigger rewards + WhatsApp.
