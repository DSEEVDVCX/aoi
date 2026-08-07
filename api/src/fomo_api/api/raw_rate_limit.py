from __future__ import annotations

import json
import logging
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fomo_api.config import settings

logger = logging.getLogger(__name__)


class RawASGIRateLimiter:
    """Pure ASGI rate-limit middleware — no BaseHTTPMiddleware overhead.
    Checks BEFORE invoking downstream. Fails closed (503) on Redis error."""

    def __init__(
        self, app: ASGIApp, redis: Any | None = None, limit_per_minute: int | None = None
    ) -> None:
        self._app = app
        self._redis = redis  # None = resolve lazily via get_redis() per request
        self._limit = limit_per_minute  # None = read from settings per-request

    def _get_redis(self) -> Any:
        if self._redis is not None:
            return self._redis
        from fomo_api.redis_state import get_redis

        return get_redis()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        limit = self._limit if self._limit is not None else settings.rate_limit_per_minute

        headers = dict(scope.get("headers", []))
        auth = (headers.get(b"authorization") or b"").decode("latin-1")
        consumer_key = None
        if auth.lower().startswith("bearer "):
            consumer_key = auth.split(" ", 1)[1].strip() or None

        if not consumer_key:
            client = scope.get("client")
            fwd = headers.get(b"x-forwarded-for")
            if fwd:
                consumer_key = fwd.decode("latin-1").split(",")[0].strip()
            elif client:
                consumer_key = client[0]
            else:
                consumer_key = "anonymous"

        bucket = int(time.time() // 60)
        key = f"rl:{consumer_key}:{bucket}"

        try:
            redis = self._get_redis()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, 60)
        except Exception:
            logger.error("Rate-limit Redis error; failing closed")
            await _send_json(send, 503, {"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "Rate-limiting service unavailable", "details": {}}})
            return

        if count > limit:
            await _send_json(
                send, 429,
                {"error": {"code": "RATE_LIMITED", "message": "Per-consumer rate limit exceeded", "details": {"limit": limit}}},
                extra_headers={
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                hdrs = message.get("headers", [])
                hdrs.append((b"x-ratelimit-limit", str(limit).encode()))
                hdrs.append((b"x-ratelimit-remaining", str(max(0, limit - count)).encode()))
                message["headers"] = hdrs
            await send(message)

        await self._app(scope, receive, send_wrapper)


async def _send_json(
    send: Send,
    status: int,
    body: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> None:
    payload = json.dumps(body).encode()
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
