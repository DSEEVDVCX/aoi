"""اختبارات عميل NodeReal بلا شبكة أو مفاتيح حقيقية."""
from __future__ import annotations

import pytest

import nodereal_rpc


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
async def test_rate_limit_error_is_distinguished():
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
