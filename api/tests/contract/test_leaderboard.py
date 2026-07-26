from __future__ import annotations

import pytest

from fomo_api.config import settings

LB = settings.upstream_leaderboard_path


@pytest.mark.asyncio
async def test_leaderboard_ranked_list(authed_client, respx_mock, fomo_json, leaderboard_payload):
    respx_mock.get(LB).mock(return_value=fomo_json(leaderboard_payload))
    r = await authed_client.get("/v1/leaderboard?page=1&page_size=20")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["data"], list)
    assert body["data"][0]["id"] == "t_1"
    assert body["data"][0]["rank"] == 1
    assert "last_refreshed_at" in body
    assert body["page"]["page"] == 1
    assert body["page"]["page_size"] == 20
    # Upstream has no total count; total_items reflects the rows fetched.
    assert body["page"]["total_items"] == 2


@pytest.mark.asyncio
async def test_leaderboard_pagination_contiguous(authed_client, respx_mock, fomo_json, leaderboard_payload):
    page2 = {
        "success": True,
        "message": "ok",
        "responseObject": {
            "leaderboard": [{**leaderboard_payload["responseObject"]["leaderboard"][0], "id": "t_3"}]
        },
        "statusCode": 200,
    }
    respx_mock.get(LB).mock(
        side_effect=[fomo_json(leaderboard_payload), fomo_json(page2)]
    )
    r1 = await authed_client.get("/v1/leaderboard?page=1&page_size=20")
    r2 = await authed_client.get("/v1/leaderboard?page=2&page_size=20")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["page"]["page"] == 1
    assert r2.json()["page"]["page"] == 2


@pytest.mark.asyncio
async def test_leaderboard_requires_auth(app_client, respx_mock, fomo_json, leaderboard_payload):
    respx_mock.get(LB).mock(return_value=fomo_json(leaderboard_payload))
    async with app_client as client:
        r = await client.get("/v1/leaderboard")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_leaderboard_upstream_5xx(authed_client, respx_mock, fomo_json):
    respx_mock.get(LB).mock(return_value=fomo_json({"err": "x"}, status=503))
    r = await authed_client.get("/v1/leaderboard")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
