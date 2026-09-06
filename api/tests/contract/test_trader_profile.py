from __future__ import annotations

import pytest

from fomo_api.config import settings

# Real Privy user ids are uuids, and the batch path is matched by path alone —
# the ids travel as repeated `userIds` query params, which respx leaves free.
T1 = "11111111-1111-5111-8111-111111111111"
T9 = "99999999-9999-5999-8999-999999999999"
BATCH = settings.upstream_traders_batch_path


def _batch(*users: dict) -> dict:
    """The CONFIRMED batch envelope: {responseObject: {users: [...]}}.

    An id upstream does not know is simply absent from `users` — it is never
    reported as an error, which is exactly what let the death of
    `/v2/users/{id}` read as "no such trader" for 21 hours.
    """
    return {"success": True, "message": "ok", "responseObject": {"users": list(users)},
            "statusCode": 200}


@pytest.mark.asyncio
async def test_trader_profile(authed_client, respx_mock, fomo_json, trader_payload):
    respx_mock.get(BATCH).mock(return_value=fomo_json(_batch({**trader_payload, "id": T1})))
    r = await authed_client.get(f"/v1/traders/{T1}")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["id"] == T1
    assert body["data"]["handle"] == "legend"
    # Upstream exposes total PnL and volume (not a percentage/win-rate).
    assert body["data"]["metrics"]["realized_pnl_usd"] == 42500.0
    assert body["data"]["metrics"]["volume_usd"] == 98000.0
    assert body["data"]["metrics"]["pnl_pct"] is None
    assert "last_refreshed_at" in body


@pytest.mark.asyncio
async def test_trader_profile_asks_the_batch_path_for_one_id(
    authed_client, respx_mock, fomo_json, trader_payload
):
    """One profile is a batch of one — `/v2/users/{id}` must never be called.

    It answers 404 for every well-formed uuid since 2026-08-19T14:53Z, so a
    second code path here would be a second thing to notice next time.
    """
    route = respx_mock.get(BATCH).mock(
        return_value=fomo_json(_batch({**trader_payload, "id": T1}))
    )
    r = await authed_client.get(f"/v1/traders/{T1}")
    assert r.status_code == 200
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.path == BATCH
    assert request.url.params.get_list("userIds") == [T1]


@pytest.mark.asyncio
async def test_trader_profile_missing_metrics_are_null(authed_client, respx_mock, fomo_json):
    payload = {"id": T9, "userHandle": "ghost", "followers": 0}
    respx_mock.get(BATCH).mock(return_value=fomo_json(_batch(payload)))
    r = await authed_client.get(f"/v1/traders/{T9}")
    assert r.status_code == 200
    metrics = r.json()["data"]["metrics"]
    assert metrics["pnl_pct"] is None
    assert metrics["win_rate_pct"] is None
    assert metrics["volume_usd"] is None
    assert metrics["realized_pnl_usd"] is None


@pytest.mark.asyncio
async def test_trader_not_found(authed_client, respx_mock, fomo_json):
    """Absent from `users` is the batch path's way of saying "no such user"."""
    respx_mock.get(BATCH).mock(return_value=fomo_json(_batch()))
    r = await authed_client.get(f"/v1/traders/{T9}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_an_id_that_is_not_a_uuid_never_reaches_upstream(
    authed_client, respx_mock, fomo_json, trader_payload
):
    """A malformed id 400s the WHOLE batch upstream, so it is filtered first.

    Here the batch is one, but the same filter guards the hundred: one bad id
    from a caller must not cost the 99 good ones sharing its call.
    """
    route = respx_mock.get(BATCH).mock(
        return_value=fomo_json(_batch({**trader_payload, "id": T1}))
    )
    r = await authed_client.get("/v1/traders/not-a-uuid")
    assert r.status_code == 404
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_trader_profile_requires_auth(app_client, respx_mock, fomo_json, trader_payload):
    respx_mock.get(BATCH).mock(return_value=fomo_json(_batch({**trader_payload, "id": T1})))
    async with app_client as client:
        r = await client.get(f"/v1/traders/{T1}")
    assert r.status_code == 401
