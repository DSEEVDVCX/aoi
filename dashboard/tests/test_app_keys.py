"""حراس HTTP وإدارة المفاتيح عبر API اللوحة، بمخزن مؤقت وأسرار وهمية."""
import re

from fastapi.testclient import TestClient

import app as dashboard_app
import config
import helius_keys as hk

HOST = {"Host": "127.0.0.1:8090"}
KEY_A = "api-test-aaaaaaaaaaaaaaaa"
KEY_B = "api-test-bbbbbbbbbbbbbbbb"


def _client_with_token():
    client = TestClient(dashboard_app.app)
    response = client.get("/", headers=HOST)
    assert response.status_code == 200
    match = re.search(r'const DASHBOARD_TOKEN = "([0-9a-f]{64})"', response.text)
    assert match is not None
    headers = {
        **HOST,
        "Origin": "http://127.0.0.1:8090",
        "X-Dashboard-Token": match.group(1),
    }
    return client, headers


def test_host_guard_rejects_dns_rebinding_name():
    client = TestClient(dashboard_app.app)
    response = client.get("/api/helius-keys", headers={"Host": "evil.example:8090"})
    assert response.status_code == 403


def test_key_api_masks_secrets_and_requires_csrf(tmp_path, monkeypatch):
    path = tmp_path / "helius-keys.json"
    monkeypatch.setattr(config, "HELIUS_KEYS_PATH", str(path))
    monkeypatch.setattr(config, "SOLANA_RPC_URL", hk.HELIUS_HTTP_BASE + KEY_A)
    client, headers = _client_with_token()

    rejected = client.post(
        "/api/helius-keys", headers=HOST,
        json={"username": "one", "apiKey": KEY_A},
    )
    assert rejected.status_code == 403

    added = client.post(
        "/api/helius-keys", headers=headers,
        json={"username": "one", "apiKey": KEY_A},
    )
    assert added.status_code == 200
    assert KEY_A not in added.text
    payload = added.json()
    assert payload["keys"][0]["primary"] is True
    identifier = payload["keys"][0]["id"]

    listed = client.get("/api/helius-keys", headers=HOST)
    assert listed.status_code == 200
    assert KEY_A not in listed.text
    assert "apiKey" not in listed.json()["keys"][0]

    disabled = client.post(
        "/api/helius-keys/disable", headers=headers,
        json={"id": identifier, "reason": "test"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["keys"][0]["disabled"] is True

    enabled = client.post(
        "/api/helius-keys/enable", headers=headers, json={"id": identifier}
    )
    assert enabled.status_code == 200
    assert enabled.json()["keys"][0]["disabled"] is False

    deleted = client.post(
        "/api/helius-keys/delete", headers=headers, json={"id": identifier}
    )
    assert deleted.status_code == 200
    assert deleted.json()["keys"] == []


def test_verify_uses_server_side_secret_and_returns_only_safe_result(
    tmp_path, monkeypatch,
):
    path = tmp_path / "helius-keys.json"
    store = {"keys": []}
    hk.add_key(store, "verify", KEY_B)
    hk.save_store(store, path)
    monkeypatch.setattr(config, "HELIUS_KEYS_PATH", str(path))
    monkeypatch.setattr(config, "SOLANA_RPC_URL", None)
    seen = []

    async def fake_verify(api_key, timeout_seconds=6.0):
        seen.append((api_key, timeout_seconds))
        return {"ok": True, "status": "ok", "message": "المفتاح يعمل ✓"}

    monkeypatch.setattr(hk, "verify_key", fake_verify)
    client, headers = _client_with_token()
    identifier = hk.list_masked(hk.load_store(path))[0]["id"]

    response = client.post(
        "/api/helius-keys/verify", headers=headers, json={"id": identifier}
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert KEY_B not in response.text
    assert seen == [(KEY_B, 6.0)]
