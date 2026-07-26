from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_login_requires_completed_privy_flow(app_client):
    async with app_client as client:
        r = await client.post("/v1/auth/login", json={"method": "wallet", "params": {}})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_logout_revokes_session(authed_client):
    r = await authed_client.post("/v1/auth/logout")
    assert r.status_code == 204
    r2 = await authed_client.get("/v1/leaderboard")
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_logout_requires_auth(app_client):
    async with app_client as client:
        r = await client.post("/v1/auth/logout")
    assert r.status_code == 401
