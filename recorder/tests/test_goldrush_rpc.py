import json

import httpx

import evm_rpc
import goldrush_rpc


NET = "143"
TOK = "0x350035555e10d9afaf1566aaebfced5ba6c27777"
TOPIC = evm_rpc.TRANSFER_TOPIC


def _event(block, *, topic=TOPIC, sender=TOK):
    return {
        "block_signed_at": "2025-11-25T23:20:03Z",
        "block_height": block,
        "tx_offset": 5,
        "log_offset": 44,
        "tx_hash": "0x" + "ab" * 32,
        "raw_log_topics": [
            topic,
            "0x" + "0" * 64,
            "0x" + "0" * 24 + "11" * 20,
        ],
        "raw_log_data": "0x01",
        "sender_address": sender,
    }


async def _noop(_seconds):
    return None


async def test_goldrush_filters_and_maps_transfer_logs(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    seen = []

    def handler(request):
        seen.append(request)
        items = [
            _event(10),
            _event(20, topic="0x" + "ff" * 32),
            _event(30, sender="0x" + "22" * 20),
        ]
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": items},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        end = goldrush_rpc.config.GOLDRUSH_BLOCK_CHUNK - 1
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, end, max_calls=10, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert calls == 1
    assert complete is True
    assert resume == end
    assert [int(log["blockNumber"], 16) for log in logs] == [10]
    assert logs[0]["address"] == TOK
    assert logs[0]["blockTimestamp"] == hex(1764112803)
    assert all(request.headers["authorization"] == "Bearer secret" for request in seen)
    assert seen[0].url.params["address"] == TOK
    assert seen[0].url.params["topics"] == TOPIC
    assert seen[0].url.params["starting-block"] == "earliest"


async def test_goldrush_keeps_only_completed_chunks_at_call_cap(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))

    def handler(_request):
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": [_event(10)]},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, 9_999, max_calls=1, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert (len(logs), calls, complete, resume) == (1, 1, False, 2_000)


async def test_non_goldrush_network_uses_normal_rpc(monkeypatch):
    rpc = goldrush_rpc.GoldRushReplayRPC()

    async def fallback(*args, **kwargs):
        return ["fallback"], 1, True, 9

    monkeypatch.setattr(evm_rpc.EVMRPC, "get_logs_paged", fallback)
    try:
        result = await rpc.get_logs_paged("4663", [TOK], 0, 9)
    finally:
        await rpc.aclose()

    assert result == (["fallback"], 1, True, 9)


async def test_goldrush_retries_a_transient_transport_error(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_RETRIES", 1)
    monkeypatch.setattr(goldrush_rpc.config, "EVM_RATE_LIMIT_BACKOFF_SECONDS", 0)
    attempts = 0

    def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("reset")
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": [_event(10)]},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        end = goldrush_rpc.config.GOLDRUSH_BLOCK_CHUNK - 1
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, end, max_calls=5, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert attempts == 2
    assert (len(logs), calls, complete, resume) == (1, 2, True, end)


async def test_small_range_falls_back_to_normal_rpc(monkeypatch):
    rpc = goldrush_rpc.GoldRushReplayRPC()

    async def fallback(*args, **kwargs):
        return ["fallback"], 1, True, 99

    monkeypatch.setattr(evm_rpc.EVMRPC, "get_logs_paged", fallback)
    try:
        result = await rpc.get_logs_paged(NET, [TOK], 0, 9, max_calls=1)
    finally:
        await rpc.aclose()

    assert result == (["fallback"], 1, True, 99)


async def test_credit_exhaustion_disables_goldrush_and_falls_back(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    goldrush_calls = 0
    fallback_calls = []

    def handler(_request):
        nonlocal goldrush_calls
        goldrush_calls += 1
        return httpx.Response(402, json={
            "data": None,
            "error": True,
            "error_message": "Credit limit exceeded for your account.",
        })

    async def fallback(_self, network_id, addresses, from_block, to_block, **kwargs):
        fallback_calls.append((network_id, tuple(addresses), from_block, to_block))
        return [{"blockNumber": hex(from_block)}], 1, True, to_block

    monkeypatch.setattr(evm_rpc.EVMRPC, "get_logs_paged", fallback)
    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await rpc.get_logs_paged(NET, [TOK], 0, 9_999, max_calls=5, sleep=_noop)
        second = await rpc.get_logs_paged(NET, [TOK], 10_000, 19_999, max_calls=5, sleep=_noop)
    finally:
        await rpc.aclose()

    assert goldrush_calls == 1
    assert fallback_calls == [
        (NET, (TOK,), 0, 9_999),
        (NET, (TOK,), 10_000, 19_999),
    ]
    assert first == ([{"blockNumber": "0x0"}], 2, True, 9_999)
    assert second == ([{"blockNumber": hex(10_000)}], 1, True, 19_999)


async def test_goldrush_rotates_to_second_key_before_rpc_fallback(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_keys": ["bad-key", "good-key"]}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    seen = []

    def handler(request):
        auth = request.headers["authorization"]
        seen.append(auth)
        if auth == "Bearer bad-key":
            return httpx.Response(402, json={"error": True, "error_message": "credits exhausted"})
        return httpx.Response(200, json={"error": False, "data": {"items": [_event(10)]}})

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        end = goldrush_rpc.config.GOLDRUSH_BLOCK_CHUNK - 1
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, end, max_calls=5, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen == ["Bearer bad-key", "Bearer good-key"]
    assert (len(logs), calls, complete, resume) == (1, 2, True, end)
