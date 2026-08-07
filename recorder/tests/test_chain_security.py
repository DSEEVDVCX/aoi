"""اختبارات محلّل السلسلة بدوال RPC مزيّفة؛ لا شبكة ولا أسرار."""
import json
from pathlib import Path
from typing import Any

import httpx

import chain_security as cs


class FakeRpc:
    def __init__(self, handler):
        self.handler = handler
        self.trace = []

    async def call(self, method: str, params: list[Any]):
        value = self.handler(method, params)
        if isinstance(value, Exception):
            raise value
        return value


def _sol_account(extensions=None, *, mint_authority=None, freeze_authority=None):
    return {
        "value": {
            "owner": cs.TOKEN_2022_PROGRAM,
            "space": 300,
            "data": {"parsed": {"type": "mint", "info": {
                "decimals": 6,
                "supply": "1000000",
                "isInitialized": True,
                "mintAuthority": mint_authority,
                "freezeAuthority": freeze_authority,
                "extensions": extensions or [],
            }}},
        }
    }


async def test_solana_non_transferable_is_blocked():
    def handler(method, _params):
        if method == "getAccountInfo":
            return _sol_account([{"extension": "nonTransferable", "state": {}}])
        if method == "getTokenLargestAccounts":
            return {"value": []}
        return cs.RpcError("unsupported")

    result = await cs.scan_solana("mint", FakeRpc(handler))

    assert result["gate_status"] == "blocked"
    assert "non_transferable" in result["reason_codes"]
    assert result["token_standard"] == "token-2022"


async def test_solana_transfer_hook_or_authority_requires_review():
    extension = {
        "extension": "transferHook",
        "state": {"authority": "authority", "programId": None},
    }

    def handler(method, _params):
        if method == "getAccountInfo":
            return _sol_account([extension])
        if method == "getTokenLargestAccounts":
            return {"value": []}
        return cs.RpcError("unsupported")

    result = await cs.scan_solana("mint", FakeRpc(handler))

    assert result["gate_status"] == "review"
    assert "transfer_hook_configurable" in result["reason_codes"]
    assert result["dangerous_capabilities"] == ["transferHook"]


def test_helius_store_adds_enabled_keys_without_leaking_path_defaults(
    tmp_path, monkeypatch,
):
    store = tmp_path / "helius-keys.json"
    store.write_text(json.dumps({"keys": [
        {"apiKey": "enabled-key-123456789", "disabledAt": None},
        {"apiKey": "disabled-key-12345678", "disabledAt": "2026-01-01"},
        {"apiKey": "bad"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("AOI_HELIUS_KEYS_PATH", str(store))

    urls = cs._solana_rpc_urls("https://fallback.invalid")

    assert urls == (
        cs.HELIUS_HTTP_BASE + "enabled-key-123456789",
    )


def test_helius_rotation_continues_after_selected_fallback(tmp_path, monkeypatch):
    keys = ["store-key-11111111", "store-key-22222222", "store-key-33333333"]
    store = tmp_path / "helius-keys.json"
    store.write_text(json.dumps({"keys": [
        {"apiKey": key, "disabledAt": None} for key in keys
    ]}), encoding="utf-8")
    monkeypatch.setenv("AOI_HELIUS_KEYS_PATH", str(store))
    selected = cs.HELIUS_HTTP_BASE + keys[1]

    assert cs._solana_rpc_urls(selected) == (
        selected,
        cs.HELIUS_HTTP_BASE + keys[2],
        cs.HELIUS_HTTP_BASE + keys[0],
    )


def test_disabled_or_deleted_fallback_is_not_reintroduced(tmp_path, monkeypatch):
    disabled = "store-key-disabled-1111"
    enabled = "store-key-enabled-22222"
    store = tmp_path / "helius-keys.json"
    store.write_text(json.dumps({"keys": [
        {"apiKey": disabled, "disabledAt": 123},
        {"apiKey": enabled, "disabledAt": None},
    ]}), encoding="utf-8")
    monkeypatch.setenv("AOI_HELIUS_KEYS_PATH", str(store))

    urls = cs._solana_rpc_urls(cs.HELIUS_HTTP_BASE + disabled)

    assert urls == (cs.HELIUS_HTTP_BASE + enabled,)


def test_all_disabled_helius_keys_return_no_rpc(tmp_path, monkeypatch):
    key = "store-key-disabled-1111"
    store = tmp_path / "helius-keys.json"
    store.write_text(json.dumps({"keys": [
        {"apiKey": key, "disabledAt": 123},
    ]}), encoding="utf-8")
    monkeypatch.setenv("AOI_HELIUS_KEYS_PATH", str(store))

    assert cs._solana_rpc_urls(cs.HELIUS_HTTP_BASE + key) == ()


def test_solana_pool_rotates_start_between_scans(tmp_path, monkeypatch):
    keys = ["round-key-111111111", "round-key-222222222", "round-key-333333333"]
    store = tmp_path / "helius-keys.json"
    store.write_text(json.dumps({"keys": [
        {"apiKey": key, "disabledAt": None} for key in keys
    ]}), encoding="utf-8")
    monkeypatch.setenv("AOI_HELIUS_KEYS_PATH", str(store))
    monkeypatch.setattr(cs, "_SOLANA_POOL_CURSOR", 0)
    fallback = cs.HELIUS_HTTP_BASE + keys[0]

    first = cs._solana_rpc_urls(fallback, rotate_start=True)
    second = cs._solana_rpc_urls(fallback, rotate_start=True)

    assert first[0] == cs.HELIUS_HTTP_BASE + keys[0]
    assert second[0] == cs.HELIUS_HTTP_BASE + keys[1]


def test_repeated_429_disables_key_for_session_only(monkeypatch):
    url = cs.HELIUS_HTTP_BASE + "rate-limit-key-111111"
    monkeypatch.setattr(cs, "_RPC_RATE_LIMIT_STRIKES", {})
    monkeypatch.setattr(cs, "_RPC_SESSION_DEAD", set())

    for _ in range(cs._RATE_LIMIT_STRIKES_TO_DISABLE):
        cs._note_endpoint_failure(url, "rate_limited")

    assert cs._endpoint_available(url) is False
    assert url in cs._RPC_SESSION_DEAD


def test_non_helius_429_never_kills_only_evm_endpoint(monkeypatch):
    url = "https://public-evm-rpc.invalid"
    monkeypatch.setattr(cs, "_RPC_RATE_LIMIT_STRIKES", {})
    monkeypatch.setattr(cs, "_RPC_SESSION_DEAD", set())

    for _ in range(cs._RATE_LIMIT_STRIKES_TO_DISABLE + 2):
        cs._note_endpoint_failure(url, "rate_limited")

    assert cs._endpoint_available(url) is True
    assert url not in cs._RPC_SESSION_DEAD


def test_helius_deprioritized_response_is_retryable():
    assert cs._rpc_failure_kind(
        code=-32600,
        message=(
            "Request deprioritized due to number of accounts requested. "
            "Slow down requests or add filters to narrow down results"
        ),
    ) == "transient"


async def test_json_rpc_rotates_endpoints_after_429_without_tracing_urls():
    seen_hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        if request.url.host == "first.invalid":
            return httpx.Response(429, json={"error": "limited"})
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1, "result": {"value": []},
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        rpc = cs.JsonRpc(
            ("https://first.invalid", "https://second.invalid"), client
        )
        result = await rpc.call("getTokenLargestAccounts", ["mint"])

    assert result == {"value": []}
    assert seen_hosts == ["first.invalid", "second.invalid"]
    assert rpc.trace[0]["http_status"] == 429
    assert "first.invalid" not in json.dumps(rpc.trace)
    assert "second.invalid" not in json.dumps(rpc.trace)


async def test_json_rpc_rotates_on_helius_overload_beyond_old_eight_key_cap():
    seen_hosts = []
    urls = tuple(f"https://key-{index}.invalid" for index in range(10))

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        if request.url.host != "key-9.invalid":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": 1,
                "error": {
                    "code": -32603,
                    "message": "account index service overloaded, please try again.",
                },
            })
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1, "result": {"value": []},
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        rpc = cs.JsonRpc(urls, client)
        result = await rpc.call("getTokenLargestAccounts", ["mint"])

    assert result == {"value": []}
    assert len(seen_hosts) == 10
    assert rpc.trace[0]["failure_kind"] == "transient"


async def test_explicit_credit_exhaustion_disables_key_and_uses_next(
    tmp_path, monkeypatch,
):
    exhausted = "credit-key-aaaaaaaaaaaa"
    working = "credit-key-bbbbbbbbbbbb"
    store = tmp_path / "helius-keys.json"
    store.write_text(json.dumps({"keys": [
        {"apiKey": exhausted, "disabledAt": None},
        {"apiKey": working, "disabledAt": None},
    ]}), encoding="utf-8")
    monkeypatch.setenv("AOI_HELIUS_KEYS_PATH", str(store))
    urls = (
        cs.HELIUS_HTTP_BASE + exhausted,
        cs.HELIUS_HTTP_BASE + working,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if exhausted in str(request.url):
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": -32000, "message": "out of credits for this month"},
            })
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": 1, "result": 123,
        })

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        rpc = cs.JsonRpc(urls, client)
        assert await rpc.call("getSlot", []) == 123

    saved = json.loads(store.read_text(encoding="utf-8"))
    assert saved["keys"][0]["disabledAt"]
    assert saved["keys"][0]["disabledReason"] == "نفدت الحصّة تلقائياً"
    assert saved["keys"][1]["disabledAt"] is None
    assert Path(str(store) + ".bak").exists()
    assert exhausted not in json.dumps(rpc.trace)


async def test_solana_rpc_failure_is_unknown_not_review():
    def handler(method, _params):
        if method == "getAccountInfo":
            return _sol_account()
        if method == "getTokenLargestAccounts":
            return cs.RpcError("all endpoints failed")
        raise AssertionError(method)

    result = await cs.scan_solana("mint", FakeRpc(handler))

    assert result["gate_status"] == "unknown"
    assert result["reason_codes"] == ["holder_transfer_check_unavailable"]
    assert result["transfer_simulation_status"] == "unavailable"


def _evm_handler(
    *, code="0x6000", paused=False, chain=56, owner=None, logs=True,
    transfer_succeeds=True,
):
    holder = "0x" + "1" * 40

    def handler(method, params):
        if method == "eth_chainId":
            return hex(chain)
        if method == "eth_getCode":
            return code
        if method == "eth_getStorageAt":
            return "0x" + "0" * 64
        if method == "eth_blockNumber":
            return "0x1000"
        if method == "eth_getLogs":
            if not logs:
                return []
            return [{"topics": [cs.TRANSFER_TOPIC, "0x" + "0" * 64,
                                "0x" + "0" * 24 + holder[2:]]}]
        if method == "eth_call":
            data = params[0]["data"]
            if data == "0x18160ddd":
                return "0x" + hex(1_000_000)[2:].rjust(64, "0")
            if data == "0x313ce567":
                return "0x" + hex(18)[2:].rjust(64, "0")
            if data in {"0x8da5cb5b", "0x893d20e8"}:
                address = owner or cs.ZERO_EVM_ADDRESS
                return "0x" + cs._address_arg(address)
            if data == "0x5c975abb":
                return "0x" + ("1" if paused else "0").rjust(64, "0")
            if data.startswith("0x70a08231"):
                return "0x" + "64".rjust(64, "0")
            if data.startswith("0xa9059cbb"):
                return "0x" + ("1" if transfer_succeeds else "0").rjust(64, "0")
            return None
        raise AssertionError(method)

    return handler


async def test_evm_plain_renounced_contract_with_holder_transfer_passes():
    result = await cs.scan_evm("0x" + "a" * 40, FakeRpc(_evm_handler()), 56)

    assert result["gate_status"] == "pass"
    assert result["owner_renounced"] == 1
    assert result["transfer_simulation_status"] == "success"


async def test_evm_paused_contract_is_blocked():
    result = await cs.scan_evm(
        "0x" + "a" * 40, FakeRpc(_evm_handler(paused=True)), 56
    )

    assert result["gate_status"] == "blocked"
    assert "contract_paused" in result["reason_codes"]


async def test_evm_failed_holder_transfer_requires_review_not_global_block():
    result = await cs.scan_evm(
        "0x" + "a" * 40,
        FakeRpc(_evm_handler(transfer_succeeds=False)),
        56,
    )

    assert result["gate_status"] == "review"
    assert result["transfer_simulation_status"] == "failed"
    assert "holder_transfer_rejected" in result["reason_codes"]


async def test_evm_minimal_proxy_and_active_owner_require_review():
    implementation = "b" * 40
    proxy = "0x363d3d373d3d3d363d73" + implementation + "5af43d82803e903d91602b57fd5bf3"

    def handler(method, params):
        if method == "eth_getCode" and params[0] == "0x" + implementation:
            return "0x600040c10f19"
        return _evm_handler(code=proxy, owner="0x" + "2" * 40)(method, params)

    result = await cs.scan_evm("0x" + "a" * 40, FakeRpc(handler), 56)

    assert result["gate_status"] == "review"
    assert result["upgradeable"] == 1
    assert result["program_or_implementation"] == "0x" + implementation
    assert "mint(address,uint256)" in result["dangerous_capabilities"]
    assert "owner_authority_active" in result["reason_codes"]


async def test_evm_chain_mismatch_is_unknown():
    result = await cs.scan_evm(
        "0x" + "a" * 40, FakeRpc(_evm_handler(chain=8453)), 56
    )

    assert result["gate_status"] == "unknown"
    assert result["reason_codes"] == ["rpc_chain_id_mismatch"]


async def test_evm_missing_contract_is_blocked():
    result = await cs.scan_evm(
        "0x" + "a" * 40, FakeRpc(_evm_handler(code="0x")), 56
    )

    assert result["gate_status"] == "blocked"
    assert result["reason_codes"] == ["contract_not_found"]


def test_fomo_hyperliquid_id_maps_to_hyperevm_chain_999():
    spec = cs.network_spec("1337")
    assert spec is not None
    assert spec.rpc_chain_id == 999


def test_solana_transfer_transaction_is_structurally_serialized():
    tx = cs._build_solana_transfer_checked(
        "11111111111111111111111111111111",
        "So11111111111111111111111111111111111111112",
        "11111111111111111111111111111111",
        "11111111111111111111111111111111",
        cs.SPL_TOKEN_PROGRAM,
        9,
    )
    assert tx[0] == 1       # توقيع واحد
    assert tx[65:69] == bytes([1, 0, 2, 5])  # message header + عدد المفاتيح
    assert tx[-10] == 12    # TransferChecked instruction
