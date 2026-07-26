"""Contract tests for the new CONFIRMED endpoints added alongside the real-API
fix: profile-by-handle, trades, and balances."""
from __future__ import annotations

import pytest

from fomo_api.config import settings


def _wrap(obj):
    return {"success": True, "message": "ok", "responseObject": obj, "statusCode": 200}


@pytest.mark.asyncio
async def test_trader_by_handle(authed_client, respx_mock, fomo_json):
    path = settings.upstream_trader_by_handle_path.format(handle="alice")
    respx_mock.get(path).mock(
        return_value=fomo_json({"id": "u1", "userHandle": "alice", "followers": 10})
    )
    r = await authed_client.get("/v1/traders/by-handle/alice")
    assert r.status_code == 200
    assert r.json()["data"]["handle"] == "alice"
    assert r.json()["data"]["id"] == "u1"


@pytest.mark.asyncio
async def test_trader_trades(authed_client, respx_mock, fomo_json):
    respx_mock.get(settings.upstream_trades_path).mock(
        return_value=fomo_json(
            _wrap({"activeTrades": [{"id": "tr1"}], "closedTrades": [], "closedCount": 0, "hasNextPage": False})
        )
    )
    r = await authed_client.get("/v1/traders/u1/trades")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["active_trades"] == [{"id": "tr1"}]
    assert data["closed_count"] == 0
    assert data["has_next_page"] is False


@pytest.mark.asyncio
async def test_trader_balances(authed_client, respx_mock, fomo_json):
    path = settings.upstream_balances_path.format(trader_id="u1")
    respx_mock.get(path).mock(return_value=fomo_json(_wrap({"holdings": [{"token": "0xabc", "amount": "5"}]})))
    r = await authed_client.get("/v1/traders/u1/balances")
    assert r.status_code == 200
    assert r.json()["data"]["holdings"][0]["token"] == "0xabc"
