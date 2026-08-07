from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SEEN_IDS = 10000


class SSEManager:
    """Manages Server-Sent Events for alert delivery to consumers.
    Subscribes to the Redis pub/sub channel and fans alert events to the
    consumer's stream. Sends heartbeats, dedups by Alert.id (bounded), and
    detects dead consumers (research.md Decision 3, contracts/alerts-sse.md)."""

    HEARTBEAT_INTERVAL = 15

    def __init__(self, pubsub: Any) -> None:
        self._pubsub = pubsub

    async def stream(
        self,
        subscription_id: str,
        tracked_trader_ids: list[str],
    ) -> AsyncIterator[dict[str, str]]:
        tracked_set = set(tracked_trader_ids)
        seen_ids: deque[str] = deque(maxlen=_MAX_SEEN_IDS)
        seen_set: set[str] = set()

        # Subscribe BEFORE announcing. An async generator suspends at each
        # yield, so subscribing after the "subscribed" event meant the channel
        # was not actually joined until the consumer asked for a second event —
        # anything published in that window was lost.
        pubsub_conn = await self._pubsub.subscribe()
        yield {"event": "subscribed", "data": json.dumps({"trader_ids": tracked_trader_ids})}

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        pubsub_conn.get_message(timeout=self.HEARTBEAT_INTERVAL),
                        timeout=self.HEARTBEAT_INTERVAL + 1,
                    )
                except TimeoutError:
                    yield {"event": "heartbeat", "data": json.dumps({"ts": _utcnow_iso()})}
                    continue
                if msg is None:
                    continue
                alert = _parse_alert(msg)
                if alert is None:
                    continue
                if alert.get("trader_id") not in tracked_set:
                    continue
                aid = alert.get("id", "")
                if aid in seen_set:
                    continue
                seen_ids.append(aid)
                seen_set.add(aid)
                if len(seen_set) > _MAX_SEEN_IDS:
                    evicted = seen_ids[0]
                    seen_ids.popleft()
                    seen_set.discard(evicted)
                yield {"event": "alert", "data": json.dumps(alert)}
        except asyncio.CancelledError:
            pass
        finally:
            await _close(pubsub_conn)


def _parse_alert(msg: Any) -> dict[str, Any] | None:
    if isinstance(msg, dict):
        data = msg.get("data")
    elif isinstance(msg, (bytes, str)):
        data = msg
    else:
        return None
    if isinstance(data, (bytes, bytearray)):
        data = data.decode()
    if not isinstance(data, str):
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


async def _close(pubsub_conn: Any) -> None:
    import contextlib

    if hasattr(pubsub_conn, "close"):
        with contextlib.suppress(Exception):
            await pubsub_conn.close()
    if hasattr(pubsub_conn, "unsubscribe"):
        with contextlib.suppress(Exception):
            await pubsub_conn.unsubscribe()


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()
