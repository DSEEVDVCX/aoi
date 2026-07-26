from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from fomo_api.config import settings

_REFRESH_KEY = "privy:refresh_token"
_APP_ID_KEY = "privy:app_id"
_PAT_KEY = "privy:pat"
_CLIENT_ID_KEY = "privy:client_id"
_CA_ID_KEY = "privy:ca_id"
_CREDS_TTL = 30 * 24 * 3600


class SessionStore:
    """Volatile, Redis-backed session store. Holds consumer keys -> fomo
    session tokens with a short TTL. Nothing is persisted to disk (FR-013)."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def create(self, fomo_session_token: str, ttl_seconds: int | None = None) -> str:
        consumer_key = secrets.token_urlsafe(32)
        ttl = ttl_seconds or settings.session_ttl_seconds
        await self._redis.setex(_key(consumer_key), ttl, fomo_session_token)
        await self._redis.setex(f"{_key(consumer_key)}:exp", ttl, _iso_ttl(ttl))
        return consumer_key

    async def verify(self, consumer_key: str) -> str | None:
        token = await self._redis.get(_key(consumer_key))
        if token is None:
            return None
        if isinstance(token, bytes):
            token = token.decode()
        return token

    async def revoke(self, consumer_key: str) -> None:
        await self._redis.delete(_key(consumer_key), f"{_key(consumer_key)}:exp")

    async def touch(self, consumer_key: str, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds or settings.session_ttl_seconds
        if await self._redis.exists(_key(consumer_key)):
            await self._redis.expire(_key(consumer_key), ttl)
            await self._redis.expire(f"{_key(consumer_key)}:exp", ttl)

    async def expiry_at(self, consumer_key: str) -> datetime | None:
        ttl = await self._redis.ttl(f"{_key(consumer_key)}:exp")
        if ttl is None or ttl < 0:
            return None
        return datetime.now(UTC) + timedelta(seconds=int(ttl))

    # --- Privy refresh credentials (shared across all sessions) ---

    async def store_refresh_creds(
        self,
        refresh_token: str,
        app_id: str | None,
        pat: str | None = None,
        client_id: str | None = None,
        ca_id: str | None = None,
    ) -> None:
        """Persist all Privy credentials the background refresher needs to renew
        access tokens without a browser: refresh_token + app_id + pat +
        client_id + ca_id (CONFIRMED required by POST auth.privy.io/v1/sessions)."""
        await self._redis.setex(_REFRESH_KEY, _CREDS_TTL, refresh_token)
        for key, val in (
            (_APP_ID_KEY, app_id),
            (_PAT_KEY, pat),
            (_CLIENT_ID_KEY, client_id),
            (_CA_ID_KEY, ca_id),
        ):
            if val:
                await self._redis.setex(key, _CREDS_TTL, val)

    async def _get_str(self, key: str) -> str | None:
        v = await self._redis.get(key)
        if v is None:
            return None
        if isinstance(v, bytes):
            v = v.decode()
        return v or None

    async def get_refresh_creds(self) -> tuple[str, str | None] | None:
        """Return (refresh_token, app_id) or None. Kept for backward compat."""
        rt = await self._get_str(_REFRESH_KEY)
        if not rt:
            return None
        return rt, await self._get_str(_APP_ID_KEY)

    async def get_full_refresh_creds(self) -> dict[str, str | None] | None:
        """Return all stored Privy creds needed to refresh, or None if no
        refresh_token is stored."""
        rt = await self._get_str(_REFRESH_KEY)
        if not rt:
            return None
        return {
            "refresh_token": rt,
            "app_id": await self._get_str(_APP_ID_KEY),
            "pat": await self._get_str(_PAT_KEY),
            "client_id": await self._get_str(_CLIENT_ID_KEY),
            "ca_id": await self._get_str(_CA_ID_KEY),
        }

    async def update_access_tokens(
        self,
        new_access_token: str,
        new_refresh_token: str | None = None,
        new_pat: str | None = None,
    ) -> None:
        """Update all active sessions with a fresh access token after a refresh.

        Resets each session's TTL to the full window so a long-running bot's
        consumer_key never expires while auto-renew keeps succeeding. Also
        persists the ROTATED refresh_token and pat (both change on every refresh
        call — CONFIRMED 2026-07-25) so the next renewal uses the current values.
        """
        ttl = settings.session_ttl_seconds
        keys = await self._redis.keys("session:*")
        for k in keys:
            if isinstance(k, bytes):
                k = k.decode()
            if k.endswith(":exp"):
                continue
            await self._redis.setex(k, ttl, new_access_token)
            await self._redis.setex(f"{k}:exp", ttl, _iso_ttl(ttl))
        if new_refresh_token:
            await self._redis.setex(_REFRESH_KEY, _CREDS_TTL, new_refresh_token)
        if new_pat:
            await self._redis.setex(_PAT_KEY, _CREDS_TTL, new_pat)


def _key(consumer_key: str) -> str:
    return f"session:{consumer_key}"


def _iso_ttl(ttl: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
