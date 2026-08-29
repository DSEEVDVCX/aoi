from __future__ import annotations

import pytest

from fomo_api.config import settings


def _ap(trader_id: str) -> str:
    return settings.upstream_activity_path.format(trader_id=trader_id)


def _activity_payload():
    """Real /v2/users/{id}/swaps envelope — token->token swaps."""
    return {
        "success": True,
        "message": "ok",
        "responseObject": {
            "swaps": [
                {
                    "id": "a_1",
                    "networkId": 1399811149,
                    "inTokenAddress": "0xin",
                    "outTokenAddress": "0xpunch",
                    "outHumanAmount": "1000",
                    "humanUsdAmountOut": "250.0",
                    "createdAt": "2026-07-23T19:39:00Z",
                }
            ],
            "hasNextPage": False,
        },
        "statusCode": 200,
    }


@pytest.mark.asyncio
async def test_activity_time_ordered_list(authed_client, respx_mock, fomo_json):
    respx_mock.get(_ap("t_1")).mock(return_value=fomo_json(_activity_payload()))
    r = await authed_client.get("/v1/traders/t_1/activity")
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 1
    # Swaps are surfaced with action="swap" and the acquired (out) token address.
    assert body["data"][0]["action"] == "swap"
    assert body["data"][0]["token"]["address"] == "0xpunch"
    assert body["data"][0]["amount_usd"] == 250.0
    assert "last_refreshed_at" in body
    assert body["page"]["total_items"] == 1


@pytest.mark.asyncio
async def test_activity_time_window_filter(authed_client, respx_mock, fomo_json):
    # Upstream /swaps has no time-range params; filtering is applied locally.
    # A window covering the single swap returns it; a disjoint window drops it.
    respx_mock.get(_ap("t_1")).mock(return_value=fomo_json(_activity_payload()))
    r_in = await authed_client.get(
        "/v1/traders/t_1/activity?from=2026-07-23T00:00:00Z&to=2026-07-23T23:59:59Z"
    )
    assert r_in.status_code == 200
    assert len(r_in.json()["data"]) == 1

    respx_mock.get(_ap("t_1")).mock(return_value=fomo_json(_activity_payload()))
    r_out = await authed_client.get(
        "/v1/traders/t_1/activity?from=2026-07-24T00:00:00Z&to=2026-07-24T23:59:59Z"
    )
    assert r_out.status_code == 200
    assert r_out.json()["data"] == []


@pytest.mark.asyncio
async def test_activity_empty_result(authed_client, respx_mock, fomo_json):
    respx_mock.get(_ap("t_1")).mock(
        return_value=fomo_json(
            {"success": True, "message": "ok", "responseObject": {"swaps": [], "hasNextPage": False}, "statusCode": 200}
        )
    )
    r = await authed_client.get("/v1/traders/t_1/activity")
    assert r.status_code == 200
    assert r.json()["data"] == []


@pytest.mark.asyncio
async def test_activity_trader_not_found(authed_client, respx_mock, fomo_json):
    respx_mock.get(_ap("missing")).mock(return_value=fomo_json(None, status=404))
    r = await authed_client.get("/v1/traders/missing/activity")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_activity_bad_time_range(authed_client, respx_mock, fomo_json):
    respx_mock.get(_ap("t_1")).mock(return_value=fomo_json(_activity_payload()))
    r = await authed_client.get(
        "/v1/traders/t_1/activity?from=2026-07-23T23:00:00Z&to=2026-07-23T00:00:00Z"
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_activity_rejects_an_invalid_single_time_bound(authed_client, respx_mock, fomo_json):
    respx_mock.get(_ap("t_1")).mock(return_value=fomo_json(_activity_payload()))
    r = await authed_client.get(
        "/v1/traders/t_1/activity?from=not-a-timestamp"
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_activity_compares_timezones_as_instants(authed_client, respx_mock, fomo_json):
    respx_mock.get(_ap("t_1")).mock(return_value=fomo_json(_activity_payload()))
    r = await authed_client.get(
        "/v1/traders/t_1/activity",
        params={
            "from": "2026-07-23T20:00:00+01:00",
            "to": "2026-07-23T21:00:00+01:00",
        },
    )
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1
