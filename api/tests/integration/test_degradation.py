from __future__ import annotations

import pytest

from fomo_api.config import settings

LB = settings.upstream_leaderboard_path
# A trader profile is a batch of one now; ids are uuids and travel as query
# params, so the mock matches the path and the id has to be well-formed to be
# sent at all (a malformed one 400s the whole batch upstream).
TP = settings.upstream_traders_batch_path
TRADER_ID = "11111111-1111-5111-8111-111111111111"


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
    respx_mock.get(TP).mock(return_value=fomo_json(trader_payload, status=401))
    r = await authed_client.get(f"/v1/traders/{TRADER_ID}")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
