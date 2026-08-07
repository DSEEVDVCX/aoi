from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import FastAPI
from starlette.responses import JSONResponse

from fomo_api.api.errors import register_error_handlers
from fomo_api.config import settings
from fomo_api.redis_state import close_redis, get_redis

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fomo_api.auth.credential_store import CredentialStore
    from fomo_api.auth.session import SessionStore
    from fomo_api.realtime.poller import AlertPoller


def _redact_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "unknown"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{host}{port}"
    except Exception:
        return "<redacted>"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    redis = get_redis()
    try:
        await redis.ping()
    except Exception as exc:
        if settings.dev:
            logger.warning("Redis not reachable at %s (dev mode: using in-memory store)", _redact_url(settings.redis_url))
            from fomo_api.redis_state import FakeRedis, _state
            _state["redis"] = FakeRedis()
        else:
            raise RuntimeError(f"Cannot reach Redis at {_redact_url(settings.redis_url)}; refusing to start") from exc

    from fomo_api.auth.session import SessionStore
    from fomo_api.auth.token_refresher import TokenRefresher

    session_store = SessionStore(get_redis())

    # Unattended startup: seed persisted Privy creds, refresh once, mint a stable
    # consumer key — so extraction runs with no manual login (survives restarts).
    cred_store = None
    if settings.auto_bootstrap:
        from fomo_api.auth.bootstrap import bootstrap_session
        from fomo_api.auth.credential_store import CredentialStore

        cred_store = CredentialStore(settings.credential_state_file)
        try:
            consumer_key = await bootstrap_session(session_store)
            if consumer_key:
                logger.info("Auto-bootstrap complete; unattended extraction is live")
        except Exception as exc:
            logger.warning("Auto-bootstrap failed (%s); continuing without it", exc)

    refresher = TokenRefresher(session_store, cred_store=cred_store)
    refresher.start()

    # Alert ingestion. Without this the /v1/alerts/stream endpoint accepted
    # subscriptions and then emitted nothing but heartbeats forever, because
    # nothing ever polled upstream or published to the channel.
    poller = _build_alert_poller(session_store, cred_store)
    await poller.start()

    yield

    await poller.stop()
    await refresher.stop()
    await close_redis()


def _build_alert_poller(
    session_store: SessionStore, cred_store: CredentialStore | None
) -> AlertPoller:
    """Wire the AlertPoller to the live session token and live subscriptions."""
    from fomo_api.realtime.poller import AlertPoller
    from fomo_api.realtime.pubsub import AlertPubSub
    from fomo_api.services.alert_subscription_service import AlertSubscriptionService

    async def _session_token(_trader_id: str) -> str | None:
        """One upstream session serves every tracked trader (alerts come from
        the global trading-activity feed, filtered per trader client-side).
        Disk is the freshest source: TokenRefresher writes each renewal there."""
        if cred_store is not None:
            stored = cred_store.load()
            if stored and stored.access_token:
                return stored.access_token
        keys = await get_redis().keys("session:*")
        for key in keys:
            key_text = key.decode() if isinstance(key, bytes) else str(key)
            if not key_text.endswith(":exp"):
                return await session_store.verify(key_text.split(":", 1)[1])
        return None

    async def _tracked() -> set[str]:
        return await AlertSubscriptionService().all_tracked_trader_ids()

    return AlertPoller(AlertPubSub(), _session_token, tracked_source=_tracked)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fomo Family API",
        version="0.1.0",
        description="Unofficial read-only API for fomo.family social crypto-trading data",
        lifespan=lifespan,
    )
    register_error_handlers(app)

    from fomo_api.api.raw_rate_limit import RawASGIRateLimiter

    app.add_middleware(RawASGIRateLimiter)

    from fomo_api.api.routes import api_router

    app.include_router(api_router, prefix="/v1")

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        try:
            await get_redis().ping()
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "redis": "unavailable", "credentials": "unknown"},
            )

        # A live FastAPI process with unusable Privy credentials cannot reach the
        # upstream, yet the old health endpoint still reported "ok". Report only
        # capability flags—never token values or credential contents.
        from fomo_api.auth.credential_store import CredentialStore

        creds = CredentialStore(settings.credential_state_file).load()
        credential_ready = bool(creds and creds.access_token and creds.is_refreshable())
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok" if credential_ready else "degraded",
                "redis": "ok",
                "credentials": "ready" if credential_ready else "unavailable",
            },
        )

    return app


app = create_app()
