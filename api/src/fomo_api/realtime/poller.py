from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fomo_api.config import settings
from fomo_api.redis_state import get_redis

logger = logging.getLogger(__name__)

WATERMARK_PREFIX = "fomo:watermark:"


class AlertPoller:
    """Polls fomo's alert endpoints per tracked trader at a short interval,
    compares new activity against the last-seen watermark, and publishes
    only alerts whose timestamp is strictly greater than the watermark
    (US3 acceptance 2: no false positives / no duplicates).
    """

    def __init__(
        self,
        pubsub: Any,
        session_token_lookup: Callable[[str], Awaitable[str | None]],
        poll_interval: int | None = None,
        tracked_source: Callable[[], Awaitable[set[str]]] | None = None,
    ) -> None:
        self._pubsub = pubsub
        self._session_token_lookup = session_token_lookup
        self._interval = poll_interval or settings.alert_poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._tracked: set[str] = set()
        self._stop = asyncio.Event()
        # Optional async callable -> set[str]. Consumers create subscriptions at
        # any time, so the poller re-reads who to watch each round instead of
        # relying on someone calling track() at the right moment.
        self._tracked_source = tracked_source

    def track(self, trader_id: str) -> None:
        self._tracked.add(trader_id)

    def untrack(self, trader_id: str) -> None:
        self._tracked.discard(trader_id)

    @property
    def tracked(self) -> set[str]:
        return set(self._tracked)

    async def _refresh_tracked(self) -> None:
        if self._tracked_source is None:
            return
        try:
            self._tracked = set(await self._tracked_source())
        except Exception:
            logger.exception("Could not refresh tracked traders; keeping previous set")

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        redis = get_redis()
        logger.info("Alert poller started (interval=%ss)", self._interval)
        last_logged: set[str] | None = None
        while not self._stop.is_set():
            await self._refresh_tracked()
            # Log only when the tracked set changes — an every-10s heartbeat
            # would bury the log, but a silent poller is unverifiable.
            if self._tracked != last_logged:
                logger.info("Alert poller tracking %d trader(s)", len(self._tracked))
                last_logged = set(self._tracked)
            if self._tracked:
                await asyncio.gather(
                    *(self._poll_trader(tid, redis) for tid in list(self._tracked)),
                    return_exceptions=True,
                )
            await asyncio.sleep(self._interval)

    async def _fetch_and_publish(self, trader_id: str, redis: Any) -> list[dict[str, Any]]:
        """Shared logic: fetch alerts, filter by watermark, publish new ones."""
        from fomo_api.clients.fomo_client import FomoClient

        token = await self._session_token_lookup(trader_id)
        if token is None:
            # Silent here would be indistinguishable from "no new alerts", which
            # is exactly how this pipeline stayed broken unnoticed.
            logger.warning("No upstream session token available; skipping poll for %s", trader_id)
            return []
        wm_key = f"{WATERMARK_PREFIX}{trader_id}"
        stored_wm = await redis.get(wm_key)
        client = FomoClient(token)
        try:
            alerts = await client.get_trader_alerts(trader_id, since_ts=stored_wm)
        finally:
            await client.aclose()

        published: list[dict[str, Any]] = []
        max_ts = stored_wm
        for alert in alerts:
            ts = alert.get("timestamp")
            if ts is None:
                continue
            if stored_wm is not None and ts <= stored_wm:
                continue
            await self._pubsub.publish(alert)
            published.append(alert)
            if max_ts is None or ts > max_ts:
                max_ts = ts

        if max_ts is not None and max_ts != stored_wm:
            await redis.set(wm_key, max_ts)

        return published

    async def _poll_trader(self, trader_id: str, redis: Any) -> None:
        try:
            await self._fetch_and_publish(trader_id, redis)
        except Exception:
            logger.exception("Error polling trader %s", trader_id)

    async def poll_once(self, trader_id: str) -> list[dict[str, Any]]:
        """Single-shot poll for testing; returns alerts published."""
        redis = get_redis()
        return await self._fetch_and_publish(trader_id, redis)
