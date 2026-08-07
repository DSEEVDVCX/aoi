"""اختبارات مخزن Helius على ملفات مؤقتة فقط؛ لا تلمس مفاتيح المستخدم."""
import json

import httpx
import pytest

import helius_keys as hk

KEY_A = "test-key-aaaaaaaaaaaaaaaa"
KEY_B = "test-key-bbbbbbbbbbbbbbbb"


def test_add_mask_disable_enable_delete_without_secret_in_listing(tmp_path):
    path = tmp_path / "helius-keys.json"
    store, _ = hk.update_store(path, lambda current: hk.add_key(current, "حساب", KEY_A))

    listed = hk.list_masked(store, hk.HELIUS_HTTP_BASE + KEY_A)
    assert len(listed) == 1
    assert listed[0]["primary"] is True
    assert listed[0]["apiKeyMasked"] != KEY_A
    assert KEY_A not in json.dumps(listed)

    identifier = listed[0]["id"]
    store, changed = hk.update_store(
        path, lambda current: hk.set_key_disabled(current, identifier, True, "quota")
    )
    assert changed is True
    assert hk.list_masked(store)[0]["disabled"] is True

    store, changed = hk.update_store(
        path, lambda current: hk.set_key_disabled(current, identifier, False)
    )
    assert changed is True
    assert hk.list_masked(store)[0]["disabled"] is False

    store, removed = hk.update_store(
        path, lambda current: hk.remove_key(current, identifier)
    )
    assert removed is True
    assert store["keys"] == []


def test_duplicate_rejected_and_lock_is_released_after_error(tmp_path):
    path = tmp_path / "helius-keys.json"
    hk.update_store(path, lambda current: hk.add_key(current, "one", KEY_A))

    with pytest.raises(ValueError, match="مضاف مسبقاً"):
        hk.update_store(path, lambda current: hk.add_key(current, "two", KEY_A))

    assert not (tmp_path / "helius-keys.json.lock").exists()
    store, _ = hk.update_store(path, lambda current: hk.add_key(current, "two", KEY_B))
    assert len(store["keys"]) == 2


def test_corrupt_current_store_recovers_from_last_valid_backup(tmp_path):
    path = tmp_path / "helius-keys.json"
    hk.update_store(path, lambda current: hk.add_key(current, "one", KEY_A))
    hk.update_store(path, lambda current: hk.add_key(current, "two", KEY_B))
    path.write_text("{broken", encoding="utf-8")

    recovered = hk.load_store(path)

    assert [item["apiKey"] for item in recovered["keys"]] == [KEY_A]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, "ok"), (401, "unauthorized"), (403, "unauthorized"), (429, "rate_limited")],
)
def test_verify_status_interpretation(status, expected):
    body = {"result": 123} if status == 200 else None
    assert hk.interpret_verify(status, body)["status"] == expected


async def test_verify_key_checks_real_largest_accounts_capability(monkeypatch):
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        methods.append(method)
        if method == "getSlot":
            return httpx.Response(200, json={"result": 123})
        return httpx.Response(429, json={"error": "limited"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def fake_client(*_args, **kwargs):
        kwargs["transport"] = transport
        return original_client(**kwargs)

    monkeypatch.setattr(hk.httpx, "AsyncClient", fake_client)

    result = await hk.verify_key(KEY_A)

    assert methods == ["getSlot", "getTokenLargestAccounts"]
    assert result["status"] == "rate_limited"
