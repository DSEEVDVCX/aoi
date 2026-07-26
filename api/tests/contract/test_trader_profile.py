from __future__ import annotations

import pytest

from fomo_api.config import settings


def _tp(trader_id: str) -> str:
    return settings.upstream_trader_path.format(trader_id=trader_id)


@pytest.mark.asyncio
async def test_trader_profile(authed_client, respx_mock, fomo_json, trader_payload):
    respx_mock.get(_tp("t_1")).mock(return_value=fomo_json(trader_payload))
    r = await authed_client.get("/v1/traders/t_1")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["id"] == "t_1"
    assert body["data"]["handle"] == "legend"
    # Upstream exposes total PnL and volume (not a percentage/win-rate).
    assert body["data"]["metrics"]["realized_pnl_usd"] == 42500.0
    assert body["data"]["metrics"]["volume_usd"] == 98000.0
    assert body["data"]["metrics"]["pnl_pct"] is None
    assert "last_refreshed_at" in body


@pytest.mark.asyncio
async def test_trader_profile_missing_metrics_are_null(authed_client, respx_mock, fomo_json):
    payload = {"id": "t_9", "userHandle": "ghost", "followers": 0}
    respx_mock.get(_tp("t_9")).mock(return_value=fomo_json(payload))
    r = await authed_client.get("/v1/traders/t_9")
    assert r.status_code == 200
    metrics = r.json()["data"]["metrics"]
    assert metrics["pnl_pct"] is None
    assert metrics["win_rate_pct"] is None
    assert metrics["volume_usd"] is None
    assert metrics["realized_pnl_usd"] is None


@pytest.mark.asyncio
async def test_trader_not_found(authed_client, respx_mock, fomo_json):
    respx_mock.get(_tp("missing")).mock(return_value=fomo_json(None, status=404))
    r = await authed_client.get("/v1/traders/missing")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_trader_profile_requires_auth(app_client, respx_mock, fomo_json, trader_payload):
    respx_mock.get(_tp("t_1")).mock(return_value=fomo_json(trader_payload))
    async with app_client as client:
        r = await client.get("/v1/traders/t_1")
    assert r.status_code == 401
