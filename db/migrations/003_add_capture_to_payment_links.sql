-- Migration 003 — payment capture tracking
-- SQLite: applied automatically by db/payment_links.py on first use.
-- PostgreSQL: run manually once.

ALTER TABLE payment_links ADD COLUMN invoice_number      TEXT;
ALTER TABLE payment_links ADD COLUMN customer_name       TEXT;
ALTER TABLE payment_links ADD COLUMN qbo_payment_id      TEXT;
ALTER TABLE payment_links ADD COLUMN payment_captured_at TEXT;
