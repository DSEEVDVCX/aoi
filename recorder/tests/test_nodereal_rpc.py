"""اختبارات عميل NodeReal بلا شبكة أو مفاتيح حقيقية."""
from __future__ import annotations

import httpx
import nodereal_rpc
import pytest


@pytest.mark.asyncio
async def test_top_holders_uses_documented_pagination_and_top_n():
    rpc = nodereal_rpc.NodeRealRPC.__new__(nodereal_rpc.NodeRealRPC)
    calls = []

    async def fake_call(method, params):
        calls.append((method, params))
        return {
            "pageKey": "ignored",
            "details": [
                {"accountAddress": "0xB", "tokenBalance": "0x64"},
                {"accountAddress": "0xA", "tokenBalance": "0xc8"},
            ],
        }

    rpc._call = fake_call
    result = await rpc.top_holders("0xTOKEN", top_n=20)

    assert calls == [("nr_getTokenHolders", ["0xtoken", "0x64", "", "0x14"])]
    assert result == [("0xb", 100), ("0xa", 200)]


@pytest.mark.asyncio
async def test_holder_count_decodes_wrapped_node_real_result():
    rpc = nodereal_rpc.NodeRealRPC.__new__(nodereal_rpc.NodeRealRPC)

    async def fake_call(method, params):
        assert method == "nr_getTokenHolderCount"
        return {"result": "0x123"}

    rpc._call = fake_call
    assert await rpc.holder_count("0xTOKEN") == 0x123


@pytest.mark.asyncio
async def test_holder_count_keeps_unsupported_contract_count_null():
    rpc = nodereal_rpc.NodeRealRPC.__new__(nodereal_rpc.NodeRealRPC)

    async def fake_call(method, params):
        return None

    rpc._call = fake_call
    assert await rpc.holder_count("0xTOKEN") is None


@pytest.mark.asyncio
async def test_rate_limit_error_is_distinguished(monkeypatch):
    # المفاتيح تُثبَّت هنا: بلا ذلك يقرأ الحوض ملفَ الجهاز فيتعلّق الاختبار به.
    monkeypatch.setattr(nodereal_rpc, "_read_keys", lambda: ["only-key"])
    monkeypatch.setattr(nodereal_rpc.config, "CHAIN_TRANSIENT_BACKOFF_SECONDS", 0)
    rpc = nodereal_rpc.NodeRealRPC.__new__(nodereal_rpc.NodeRealRPC)
    rpc._client = None

    class Response:
        status_code = 200

        def json(self):
            return {"error": {"code": -32005, "message": "CUPS exceeded"}}

    class Client:
        async def post(self, *args, **kwargs):
            return Response()

    rpc._client = Client()
    with pytest.raises(nodereal_rpc.NodeRealRateLimit):
        await rpc._call("nr_getTokenHolderCount", ["0x" + "1" * 40])


async def test_nodereal_rotates_to_second_key_on_cups_limit(monkeypatch):
    monkeypatch.setattr(nodereal_rpc, "_read_keys", lambda: ["bad-key", "good-key"])
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "bad-key" in str(request.url):
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": -32005, "message": "CUPS exceeded"},
            })
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x2a"})

    rpc = nodereal_rpc.NodeRealRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        value = await rpc.holder_count("0x" + "1" * 40)
    finally:
        await rpc.aclose()

    assert value == 42
    assert len(seen) == 2
    assert "bad-key" in seen[0] and "good-key" in seen[1]


async def test_nodereal_retries_a_transient_5xx_without_burning_the_key(monkeypatch):
    """5xx عطلُ الخدمة لا عطبُ المفتاح: كان خطأً نهائيّاً ولو كان مفتاحٌ سليم."""
    monkeypatch.setattr(nodereal_rpc, "_read_keys", lambda: ["only-key"])
    monkeypatch.setattr(nodereal_rpc.config, "CHAIN_TRANSIENT_BACKOFF_SECONDS", 0)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(503, text="upstream unavailable")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x2a"})

    rpc = nodereal_rpc.NodeRealRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        value = await rpc.holder_count("0x" + "1" * 40)
    finally:
        await rpc.aclose()

    assert value == 42
    assert len(seen) == 2
    assert rpc._keys._blocked_until == {}
