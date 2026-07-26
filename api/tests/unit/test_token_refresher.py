from __future__ import annotations

import pytest
import respx
from httpx import Response

from fomo_api.auth.session import SessionStore
from fomo_api.auth.token_refresher import TokenRefresher, _call_privy_refresh
from fomo_api.redis_state import FakeRedis


@pytest.fixture
def store():
    return SessionStore(FakeRedis())


@pytest.mark.asyncio
async def test_store_and_get_refresh_creds(store):
    await store.store_refresh_creds("rt-abc", "app-123", pat="pat-xyz")
    creds = await store.get_refresh_creds()
    assert creds == ("rt-abc", "app-123")


@pytest.mark.asyncio
async def test_get_full_refresh_creds(store):
    await store.store_refresh_creds("rt-abc", "app-123", pat="pat-xyz", client_id="client-aaa", ca_id="ca-bbb")
    full = await store.get_full_refresh_creds()
    assert full["refresh_token"] == "rt-abc"
    assert full["app_id"] == "app-123"
    assert full["pat"] == "pat-xyz"
    assert full["client_id"] == "client-aaa"
    assert full["ca_id"] == "ca-bbb"


@pytest.mark.asyncio
async def test_get_refresh_creds_returns_none_when_empty(store):
    assert await store.get_refresh_creds() is None


@pytest.mark.asyncio
async def test_update_access_tokens_updates_all_sessions(store):
    key1 = await store.create("old-token-1")
    key2 = await store.create("old-token-2")
    await store.update_access_tokens("new-token")
    assert await store.verify(key1) == "new-token"
    assert await store.verify(key2) == "new-token"


@pytest.mark.asyncio
async def test_update_access_tokens_also_updates_refresh_and_pat(store):
    await store.store_refresh_creds("rt-old", "app-1", pat="pat-old")
    await store.update_access_tokens("new-access", "rt-new", "pat-new")
    creds = await store.get_full_refresh_creds()
    assert creds["refresh_token"] == "rt-new"
    assert creds["pat"] == "pat-new"


@pytest.mark.asyncio
async def test_call_privy_refresh_success():
    with respx.mock:
        respx.post("https://auth.privy.io/api/v1/sessions").mock(
            return_value=Response(200, json={
                "token": "new-access",
                "privy_access_token": "new-pat",
                "refresh_token": "new-rt",
            })
        )
        result = await _call_privy_refresh("rt-abc", "app-123", pat="pat-xyz")
    assert result["access"] == "new-access"
    assert result["refresh"] == "new-rt"
    assert result["pat"] == "new-pat"


@pytest.mark.asyncio
async def test_call_privy_refresh_raises_on_error():
    with respx.mock:
        respx.post("https://auth.privy.io/api/v1/sessions").mock(
            return_value=Response(401, json={"error": "invalid"})
        )
        with pytest.raises(RuntimeError, match="Privy refresh failed"):
            await _call_privy_refresh("bad-rt", "app-123", pat="pat-xyz")


@pytest.mark.asyncio
async def test_refresher_does_nothing_without_creds(store):
    refresher = TokenRefresher(store)
    await refresher._maybe_refresh()


@pytest.mark.asyncio
async def test_refresher_does_nothing_without_pat(store):
    await store.store_refresh_creds("rt-abc", "app-123")  # no pat
    refresher = TokenRefresher(store)
    await refresher._maybe_refresh()  # should warn and return, not raise


@pytest.mark.asyncio
async def test_refresher_updates_sessions_on_success(store):
    await store.store_refresh_creds("rt-abc", "app-123", pat="pat-xyz")
    key = await store.create("old-token")

    with respx.mock:
        respx.post("https://auth.privy.io/api/v1/sessions").mock(
            return_value=Response(200, json={
                "token": "refreshed-token",
                "privy_access_token": "new-pat",
                "refresh_token": "rt-new",
            })
        )
        refresher = TokenRefresher(store)
        await refresher._maybe_refresh()

    assert await store.verify(key) == "refreshed-token"
    full = await store.get_full_refresh_creds()
    assert full["refresh_token"] == "rt-new"
    assert full["pat"] == "new-pat"

