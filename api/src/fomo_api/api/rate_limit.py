from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from fomo_api.config import settings

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-consumer Redis-backed rate limiter. 429 when the minute bucket overflows.
    Checks BEFORE invoking downstream so rate-limited requests never hit the upstream.
    Fails closed (503) when Redis is unavailable."""

    def __init__(
        self, app: ASGIApp, redis: Any, limit_per_minute: int | None = None
    ) -> None:
        super().__init__(app)
        self._redis = redis
        self._limit = limit_per_minute or settings.rate_limit_per_minute

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        consumer_key = _consumer_key_from(request)
        bucket_id = consumer_key or _client_ip_from(request) or "anonymous"
        bucket = int(time.time() // 60)
        key = f"rl:{bucket_id}:{bucket}"

        try:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 60)
        except Exception:
            logger.error("Rate-limit Redis error; failing closed")
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "Rate-limiting service unavailable", "details": {}}},
            )

        if count > self._limit:
            return JSONResponse(
                status_code=429,
                content={"error": {"code": "RATE_LIMITED", "message": "Per-consumer rate limit exceeded", "details": {"limit": self._limit}}},
                headers={
                    "X-RateLimit-Limit": str(self._limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, self._limit - count))
        return response


def _consumer_key_from(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


def _client_ip_from(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
