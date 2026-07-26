from __future__ import annotations

import pytest

from fomo_api.config import settings

LB = settings.upstream_leaderboard_path


@pytest.mark.asyncio
async def test_rate_limit_enforced(authed_client, respx_mock, fomo_json, leaderboard_payload, fake_redis, monkeypatch):
    from fomo_api.redis_state import get_redis

    limit = 3
    monkeypatch.setattr(settings, "rate_limit_per_minute", limit)
    redis = get_redis()
    respx_mock.get(LB).mock(return_value=fomo_json(leaderboard_payload))
    # Manually drive the counter to just below a low limit
    bucket = int(__import__("time").time() // 60)
    key = f"rl:test-key:{bucket}"
    for _ in range(limit):
        await redis.incr(key)
    r = await authed_client.get("/v1/leaderboard")
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_rate_limit_headers_present(authed_client, respx_mock, fomo_json, leaderboard_payload):
    respx_mock.get(LB).mock(return_value=fomo_json(leaderboard_payload))
    r = await authed_client.get("/v1/leaderboard")
    assert r.headers.get("x-ratelimit-limit") is not None
    assert r.headers.get("x-ratelimit-remaining") is not None
