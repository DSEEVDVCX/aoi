from __future__ import annotations

import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_alert_stream_requires_auth(app_client):
    async with app_client as client:
        r = await client.get("/v1/alerts/stream")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_alert_stream_delivers_events(authed_client, monkeypatch):
    """Verify the SSE manager publishes alert events for tracked traders
    and deduplicates by Alert.id (contracts/alerts-sse.md)."""
    from fomo_api.realtime.sse import SSEManager

    events: list[dict[str, str]] = []
    tracked = ["t_1"]

    class _FakePubSub:
        async def subscribe(self):
            return _FakeChannel()

    class _FakeChannel:
        def __init__(self) -> None:
            self._messages = [
                {"data": json.dumps({"id": "al_1", "trader_id": "t_1", "token": {"id": "x", "symbol": "P", "chain": "sol"}, "timestamp": "2026-07-23T19:39:00Z"})},
                {"data": json.dumps({"id": "al_1", "trader_id": "t_1", "token": {"id": "x", "symbol": "P", "chain": "sol"}, "timestamp": "2026-07-23T19:39:00Z"})},
                {"data": json.dumps({"id": "al_2", "trader_id": "t_2", "token": {"id": "x", "symbol": "P", "chain": "sol"}, "timestamp": "2026-07-23T19:40:00Z"})},
                None,
            ]
            self._idx = 0

        async def get_message(self, timeout=None):
            await asyncio.sleep(0.01)
            if self._idx >= len(self._messages):
                raise asyncio.CancelledError
            msg = self._messages[self._idx]
            self._idx += 1
            return msg

        async def close(self):
            pass

        async def unsubscribe(self):
            pass

    manager = SSEManager(_FakePubSub())
    async for ev in manager.stream("test-sub", tracked):
        events.append(ev)
        if len(events) >= 3:
            break

    types = [e.get("event") for e in events]
    assert "subscribed" in types
    alert_events = [e for e in events if e.get("event") == "alert"]
    alert_ids = [json.loads(e["data"])["id"] for e in alert_events]
    assert "al_1" in alert_ids
    assert alert_ids.count("al_1") == 1  # deduplicated
    assert "al_2" not in alert_ids  # t_2 not tracked
