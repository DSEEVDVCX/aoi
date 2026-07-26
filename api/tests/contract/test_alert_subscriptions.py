from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_subscription(authed_client):
    r = await authed_client.post(
        "/v1/alerts/subscriptions", json={"trader_ids": ["t_1", "t_2"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert "subscription_id" in body
    assert body["trader_ids"] == ["t_1", "t_2"]


@pytest.mark.asyncio
async def test_create_subscription_empty_rejected(authed_client):
    r = await authed_client.post("/v1/alerts/subscriptions", json={"trader_ids": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_delete_subscription(authed_client):
    r = await authed_client.post(
        "/v1/alerts/subscriptions", json={"trader_ids": ["t_1"]}
    )
    sub_id = r.json()["subscription_id"]
    r2 = await authed_client.delete(f"/v1/alerts/subscriptions/{sub_id}")
    assert r2.status_code == 204


@pytest.mark.asyncio
async def test_delete_subscription_not_found(authed_client):
    r = await authed_client.delete("/v1/alerts/subscriptions/nonexistent")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_subscription_requires_auth(app_client):
    async with app_client as client:
        r = await client.post("/v1/alerts/subscriptions", json={"trader_ids": ["t_1"]})
    assert r.status_code == 401
