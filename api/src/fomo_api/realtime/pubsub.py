from __future__ import annotations

import json
import logging
from typing import Any

from fomo_api.redis_state import get_redis

logger = logging.getLogger(__name__)

ALERT_CHANNEL = "fomo:alerts"


class AlertPubSub:
    """Publishes alert events to a Redis pub/sub channel and subscribes
    consumers to them. Decouples upstream ingestion from consumer streams
    (research.md Decision 3)."""

    def __init__(self, redis=None) -> None:
        self._redis = redis or get_redis()

    async def publish(self, alert: dict[str, Any]) -> int:
        message = json.dumps(alert)
        try:
            return await self._redis.publish(ALERT_CHANNEL, message)
        except Exception:
            logger.warning("Failed to publish alert %s", alert.get("id"))
            return 0

    async def subscribe(self):
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(ALERT_CHANNEL)
        return pubsub
