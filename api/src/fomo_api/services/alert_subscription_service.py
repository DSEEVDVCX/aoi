from __future__ import annotations

import json
import secrets
from typing import Any

from fomo_api.redis_state import get_redis

SUB_PREFIX = "fomo:sub:"
SUB_INDEX_PREFIX = "fomo:sub:consumer:"


class AlertSubscriptionService:
    """Manages alert subscriptions: tracked trader_ids per consumer.
    Stored in volatile Redis (no persistence — FR-013)."""

    def __init__(self, redis: Any | None = None) -> None:
        self._redis = redis or get_redis()

    async def create(self, consumer_key: str, trader_ids: list[str]) -> dict[str, Any]:
        if not trader_ids:
            from fomo_api.api.errors import VALIDATION_ERROR, ApiError

            raise ApiError(VALIDATION_ERROR, "trader_ids must be a non-empty array", {})
        subscription_id = secrets.token_urlsafe(16)
        data = json.dumps({"trader_ids": trader_ids})
        await self._redis.setex(f"{SUB_PREFIX}{subscription_id}", 3600, data)
        await self._redis.setex(f"{SUB_INDEX_PREFIX}{consumer_key}:{subscription_id}", 3600, "1")
        return {"subscription_id": subscription_id, "trader_ids": trader_ids}

    async def get(self, subscription_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"{SUB_PREFIX}{subscription_id}")
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)  # type: ignore[no-any-return]

    async def delete(self, subscription_id: str, consumer_key: str) -> bool:
        index_key = f"{SUB_INDEX_PREFIX}{consumer_key}:{subscription_id}"
        sub_key = f"{SUB_PREFIX}{subscription_id}"
        owned = await self._redis.exists(index_key)
        if not owned:
            return False
        await self._redis.delete(sub_key, index_key)
        return True

    async def list_for_consumer(self, consumer_key: str) -> list[str]:
        pattern = f"{SUB_INDEX_PREFIX}{consumer_key}:*"
        ids: list[str] = []
        if hasattr(self._redis, "scan_iter"):
            async for key in self._redis.scan_iter(match=pattern):
                v = key.decode() if isinstance(key, bytes) else key
                ids.append(v.rsplit(":", 1)[-1])
        return ids

    async def all_tracked_trader_ids(self) -> set[str]:
        """Every trader id across all live subscriptions.

        The AlertPoller polls this union: subscriptions come and go while it
        runs, so it re-reads the set each round rather than being told about
        each one."""
        tracked: set[str] = set()
        if not hasattr(self._redis, "scan_iter"):
            return tracked
        async for key in self._redis.scan_iter(match=f"{SUB_PREFIX}*"):
            k = key.decode() if isinstance(key, bytes) else key
            # The per-consumer index shares the prefix; only sub records hold JSON.
            if k.startswith(SUB_INDEX_PREFIX):
                continue
            sub = await self.get(k[len(SUB_PREFIX):])
            if sub:
                tracked.update(sub.get("trader_ids", []))
        return tracked
