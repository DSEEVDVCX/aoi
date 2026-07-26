from __future__ import annotations

import pytest

from fomo_api.config import settings

LB = settings.upstream_leaderboard_path
TP = settings.upstream_trader_path.format


@pytest.mark.asyncio
async def test_upstream_5xx_returns_502(authed_client, respx_mock, fomo_json):
    respx_mock.get(LB).mock(return_value=fomo_json({"err": "down"}, status=503))
    r = await authed_client.get("/v1/leaderboard")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert "upstream_status" in r.json()["error"]["details"]


@pytest.mark.asyncio
async def test_upstream_timeout_returns_502(authed_client, respx_mock):
    import httpx

    respx_mock.get(LB).mock(side_effect=httpx.ConnectTimeout("timeout"))
    r = await authed_client.get("/v1/leaderboard")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_upstream_never_fabricates(authed_client, respx_mock, fomo_json):
    """FR-007: on upstream failure the API MUST NOT return fabricated 200."""
    respx_mock.get(LB).mock(return_value=fomo_json(None, status=502))
    r = await authed_client.get("/v1/leaderboard")
    assert r.status_code == 502
    assert r.status_code != 200


@pytest.mark.asyncio
async def test_expired_session_returns_401(authed_client, respx_mock, fomo_json, trader_payload):
    respx_mock.get(TP(trader_id="t_1")).mock(return_value=fomo_json(trader_payload, status=401))
    r = await authed_client.get("/v1/traders/t_1")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
