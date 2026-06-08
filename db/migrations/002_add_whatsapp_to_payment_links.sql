-- Migration 002 — add WhatsApp tracking to payment_links
-- SQLite: applied automatically by db/payment_links.py on first use.
-- PostgreSQL: run manually once.

ALTER TABLE payment_links ADD COLUMN whatsapp_sent        INTEGER NOT NULL DEFAULT 0;
ALTER TABLE payment_links ADD COLUMN whatsapp_message_sid TEXT;
ALTER TABLE payment_links ADD COLUMN whatsapp_to_number   TEXT;
ALTER TABLE payment_links ADD COLUMN whatsapp_sent_at     TEXT;
ALTER TABLE payment_links ADD COLUMN whatsapp_error       TEXT;
