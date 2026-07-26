"""Unit tests for the unattended-auth credential persistence layer."""
from __future__ import annotations

import json

from fomo_api.auth.credential_store import CredentialStore, StoredCredentials


def test_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    store = CredentialStore(path)
    assert store.load() is None  # nothing yet

    creds = StoredCredentials(
        access_token="acc", refresh_token="ref", pat="pat",
        app_id="app123", client_id="client-xxx", ca_id="ca1",
    )
    store.save(creds)
    back = store.load()
    assert back is not None
    assert back.refresh_token == "ref"
    assert back.pat == "pat"
    assert back.app_id == "app123"
    assert back.is_refreshable() is True


def test_partial_save_merges(tmp_path):
    """A rotation-only save (refresh+pat) must not wipe app_id/ca_id."""
    path = str(tmp_path / "state.json")
    store = CredentialStore(path)
    store.save(StoredCredentials(refresh_token="r1", pat="p1", app_id="app", ca_id="ca"))
    # Simulate a background renewal: only the rotated secrets are supplied.
    store.save(StoredCredentials(access_token="a2", refresh_token="r2", pat="p2"))
    back = store.load()
    assert back.access_token == "a2"
    assert back.refresh_token == "r2"
    assert back.pat == "p2"
    assert back.app_id == "app"   # preserved
    assert back.ca_id == "ca"     # preserved


def test_from_local_storage_dump(tmp_path):
    dump = {
        "_full_localStorage": {
            "privy:token": '"tok"',
            "privy:refresh_token": '"ref"',
            "privy:pat": "pat",
            "privy:caid": "ca",
            "privy:cm6h485o300n3zj9yl6vpedq7:recent-login-method": "email",
            "privy:connections": "…client-ABCDEFGHIJKLMNOPQRSTUV…",
        }
    }
    p = tmp_path / "dump.json"
    p.write_text(json.dumps(dump), encoding="utf-8")
    creds = CredentialStore.from_local_storage_dump(str(p))
    assert creds is not None
    assert creds.access_token == "tok"        # surrounding quotes stripped
    assert creds.refresh_token == "ref"
    assert creds.app_id == "cm6h485o300n3zj9yl6vpedq7"
    assert creds.client_id == "client-ABCDEFGHIJKLMNOPQRSTUV"
    assert creds.is_refreshable() is True


def test_dump_missing_returns_none(tmp_path):
    assert CredentialStore.from_local_storage_dump(str(tmp_path / "nope.json")) is None
