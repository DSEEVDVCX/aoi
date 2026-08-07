from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import redis.asyncio as aioredis

from fomo_api.config import settings

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {"redis": None}


def get_redis() -> aioredis.Redis:
    if _state["redis"] is None:
        _state["redis"] = aioredis.from_url(settings.redis_url, decode_responses=True)
    return cast(aioredis.Redis, _state["redis"])


async def close_redis() -> None:
    client = _state["redis"]
    if client is not None:
        await client.aclose()
        _state["redis"] = None


class FakeRedis:
    """In-memory stand-in for Redis covering the subset the app uses.

    Used by tests AND by dev mode when a real Redis is unreachable, so it has to
    support every call the app makes — a missing method degrades silently into a
    dead feature rather than a loud error. `set`, `scan`/`scan_iter` and a real
    pub/sub were added for exactly that reason: without `scan_iter`
    AlertSubscriptionService.list_for_consumer returned [] and the alert stream
    tracked nothing; without a delivering pub/sub, published alerts vanished.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._ttls: dict[str, float] = {}
        self._subscribers: list[_FakePubSub] = []

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str) -> bool:
        self._store[key] = value
        return True

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value
        self._ttls[key] = float(ttl)

    async def expire(self, key: str, ttl: int) -> None:
        if key in self._store:
            self._ttls[key] = float(ttl)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                self._ttls.pop(k, None)
                removed += 1
        return removed

    async def exists(self, key: str) -> bool:
        return key in self._store

    async def incr(self, key: str) -> int:
        v = int(self._store.get(key, "0")) + 1
        self._store[key] = str(v)
        return v

    async def ttl(self, key: str) -> int:
        return int(self._ttls.get(key, -1))

    async def publish(self, channel: str, message: str) -> int:
        """Fan the message out to every live subscriber of `channel`."""
        delivered = 0
        for sub in list(self._subscribers):
            if sub.deliver(channel, message):
                delivered += 1
        return delivered

    def pubsub(self) -> _FakePubSub:
        sub = _FakePubSub(self)
        self._subscribers.append(sub)
        return sub

    def _drop_subscriber(self, sub: _FakePubSub) -> None:
        with contextlib.suppress(ValueError):  # already removed
            self._subscribers.remove(sub)

    async def aclose(self) -> None:
        pass

    async def keys(self, pattern: str = "*") -> list[str]:
        import fnmatch
        return [k for k in self._store if fnmatch.fnmatch(k, pattern)]

    async def scan(
        self, cursor: int = 0, match: str | None = None, count: int | None = None
    ) -> tuple[int, list[str]]:
        """Single-pass scan: returns (0, keys) since the store fits in memory."""
        return 0, await self.keys(match or "*")

    async def scan_iter(
        self, match: str | None = None, count: int | None = None
    ) -> AsyncIterator[str]:
        for key in await self.keys(match or "*"):
            yield key

    async def ping(self) -> bool:
        return True


class _FakePubSub:
    """In-memory pub/sub connection with the redis-py async surface we use."""

    def __init__(self, redis: FakeRedis | None = None) -> None:
        self._redis = redis
        self._channels: set[str] = set()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def subscribe(self, *channels: str) -> None:
        self._channels.update(channels)

    async def unsubscribe(self, *channels: str) -> None:
        self._channels.difference_update(channels or tuple(self._channels))

    def deliver(self, channel: str, message: str) -> bool:
        """Called by FakeRedis.publish; queues the message if subscribed."""
        if channel not in self._channels:
            return False
        self._queue.put_nowait({"type": "message", "channel": channel, "data": message})
        return True

    async def get_message(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Mirrors redis-py: returns None when nothing arrives within `timeout`."""
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def close(self) -> None:
        if self._redis is not None:
            self._redis._drop_subscriber(self)
        self._channels.clear()
