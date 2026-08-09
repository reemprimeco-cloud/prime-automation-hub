"""Tests for auth.token_store bootstrap helpers."""
from __future__ import annotations

import base64
import json

from auth.token_store import bootstrap_from_env, force_bootstrap_requested


def test_bootstrap_from_env_accepts_unpadded_base64(tmp_path, monkeypatch):
    payload = {
        "access_token": "at",
        "refresh_token": "rt",
        "realm_id": "123",
        "access_token_expires_at": 1.0,
        "refresh_token_expires_at": 2.0,
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    token_path = str(tmp_path / "tokens.json")

    monkeypatch.setenv("QBO_TOKENS_JSON", encoded)
    assert bootstrap_from_env(token_path) is True

    with open(token_path, encoding="utf-8") as fh:
        assert json.load(fh) == payload


def test_bootstrap_from_env_accepts_raw_json(tmp_path, monkeypatch):
    payload = {
        "access_token": "at",
        "refresh_token": "rt",
        "realm_id": "123",
        "access_token_expires_at": 1.0,
        "refresh_token_expires_at": 2.0,
    }
    token_path = str(tmp_path / "tokens.json")
    monkeypatch.setenv("QBO_TOKENS_JSON", json.dumps(payload))
    assert bootstrap_from_env(token_path) is True
    with open(token_path, encoding="utf-8") as fh:
        assert json.load(fh)["refresh_token"] == "rt"


def test_bootstrap_from_env_force_overwrites_existing_file(tmp_path, monkeypatch):
    token_path = str(tmp_path / "tokens.json")
    with open(token_path, "w", encoding="utf-8") as fh:
        json.dump({"access_token": "old"}, fh)

    payload = {
        "access_token": "new",
        "refresh_token": "rt",
        "realm_id": "123",
        "access_token_expires_at": 1.0,
        "refresh_token_expires_at": 2.0,
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    monkeypatch.setenv("QBO_TOKENS_JSON", encoded)

    assert bootstrap_from_env(token_path) is False
    assert bootstrap_from_env(token_path, force=True) is True
    with open(token_path, encoding="utf-8") as fh:
        assert json.load(fh)["access_token"] == "new"


def test_force_bootstrap_env_var_overwrites_existing_file(tmp_path, monkeypatch):
    token_path = str(tmp_path / "tokens.json")
    with open(token_path, "w", encoding="utf-8") as fh:
        json.dump({"access_token": "old", "refresh_token": "dead"}, fh)

    payload = {
        "access_token": "new",
        "refresh_token": "fresh",
        "realm_id": "123",
        "access_token_expires_at": 1.0,
        "refresh_token_expires_at": 2.0,
    }
    monkeypatch.setenv("QBO_TOKENS_JSON", json.dumps(payload))

    # Without the flag the existing file wins, as before.
    assert bootstrap_from_env(token_path) is False

    monkeypatch.setenv("QBO_TOKENS_FORCE_BOOTSTRAP", "1")
    assert force_bootstrap_requested() is True
    assert bootstrap_from_env(token_path) is True
    with open(token_path, encoding="utf-8") as fh:
        assert json.load(fh)["refresh_token"] == "fresh"


def test_force_bootstrap_requested_ignores_unset_and_falsy_values(monkeypatch):
    monkeypatch.delenv("QBO_TOKENS_FORCE_BOOTSTRAP", raising=False)
    assert force_bootstrap_requested() is False
    for value in ("", "0", "false", "no"):
        monkeypatch.setenv("QBO_TOKENS_FORCE_BOOTSTRAP", value)
        assert force_bootstrap_requested() is False
