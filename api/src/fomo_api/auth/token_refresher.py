from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

logger = logging.getLogger(__name__)

_PRIVY_TOKEN_URL = "https://auth.privy.io/api/v1/sessions"
_REFRESH_BEFORE_EXPIRY = 300  # renew 5 min before expiry
_POLL_INTERVAL = 60


class TokenRefresher:
    """Background task that renews the Privy access token using the stored
    refresh token, then updates all active sessions in Redis."""

    def __init__(self, session_store, cred_store=None) -> None:
        self._store = session_store
        # Optional CredentialStore: when present, every successful renewal is
        # also mirrored to disk so the ROTATED refresh_token + pat survive
        # restarts and the extractor keeps running with zero manual logins.
        self._cred_store = cred_store
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="token-refresher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self._maybe_refresh()
            except Exception as exc:
                logger.warning("Token refresh error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _maybe_refresh(self) -> None:
        creds = await self._store.get_full_refresh_creds()
        if not creds:
            return
        app_id = creds.get("app_id")
        pat = creds.get("pat")
        if not app_id:
            logger.warning("No Privy app_id stored; cannot refresh token")
            return
        if not pat:
            logger.warning("No Privy pat stored; cannot authorize refresh call")
            return
        # Keep the current access token so we don't drop it when Privy replies
        # session_update_action=ignore (token: null) — it stays valid.
        current_access = None
        if self._cred_store is not None:
            stored = self._cred_store.load()
            if stored:
                current_access = stored.access_token
        result = await _call_privy_refresh(
            refresh_token=creds["refresh_token"],
            app_id=app_id,
            pat=pat,
            client_id=creds.get("client_id"),
            ca_id=creds.get("ca_id"),
            current_access=current_access,
        )
        await self._store.update_access_tokens(
            result["access"], result.get("refresh"), result.get("pat")
        )
        if self._cred_store is not None:
            try:
                from fomo_api.auth.credential_store import StoredCredentials

                self._cred_store.save(
                    StoredCredentials(
                        access_token=result.get("access"),
                        refresh_token=result.get("refresh"),
                        pat=result.get("pat"),
                    )
                )
            except Exception as exc:  # never let disk issues break renewal
                logger.warning("Could not persist rotated creds to disk: %s", exc)
        logger.info("Privy access token refreshed successfully")


_PRIVY_CLIENT = "react-auth:3.34.0"


async def _call_privy_refresh(
    refresh_token: str,
    app_id: str,
    pat: str | None = None,
    client_id: str | None = None,
    ca_id: str | None = None,
    current_access: str | None = None,
) -> dict[str, str | None]:
    """Renew the Privy session WITHOUT a browser.

    CONFIRMED against live Privy API (2026-07-25): POST
    https://auth.privy.io/api/v1/sessions with body {"refresh_token": ...} and
    headers:
        authorization: Bearer <pat>   (privy:pat, att="pat")
        privy-app-id, privy-client: react-auth:3.34.0,
        privy-client-id (REQUIRED — omitting it -> 400), privy-ca-id
    returns HTTP 200 with keys {user, token, privy_access_token, refresh_token,
    session_update_action}.

    IMPORTANT (CONFIRMED): the response only contains a fresh app access token in
    `token` when Privy decides the session needs updating. While the current
    access token is still valid, Privy returns `token: null` and
    `session_update_action: "ignore"` — in that case the CURRENT access token
    remains valid and must be kept. `privy_access_token` is the rotated PAT
    (aud=auth.privy.io, att=pat), NOT an app token, so it must never be used as
    the fomo access token. Both `privy_access_token` and `refresh_token` rotate
    every call and must be persisted.

    Returns {"access", "refresh", "pat"} — `access` is a valid fomo app access
    token (the fresh `token` when present, else `current_access`).
    """
    headers: dict[str, str] = {
        "content-type": "application/json",
        "privy-app-id": app_id,
        "privy-client": _PRIVY_CLIENT,
        "origin": "https://fomo.family",
        "referer": "https://fomo.family/",
    }
    if pat:
        headers["authorization"] = f"Bearer {pat}"
    # privy-client-id is REQUIRED for a 200 (CONFIRMED: omitting it -> 400
    # "Invalid auth token"). It is a stable per-app id, so fall back to the
    # configured default when the caller did not capture one.
    if not client_id:
        from fomo_api.config import settings

        client_id = settings.privy_client_id or None
    if client_id:
        headers["privy-client-id"] = client_id
    if ca_id:
        headers["privy-ca-id"] = ca_id

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _PRIVY_TOKEN_URL,
            json={"refresh_token": refresh_token},
            headers=headers,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Privy refresh failed: HTTP {resp.status_code} — {resp.text[:200]}")
    data = resp.json()
    # Privy returns a fresh app access token in `token` only when the session
    # needs updating; otherwise `token` is null and the CURRENT access token is
    # still valid. Never fall back to `privy_access_token` here — that is the PAT
    # (aud=auth.privy.io), which fomo rejects.
    access = data.get("token") or current_access
    if not access:
        raise RuntimeError(
            "Privy refresh returned no app token and no current access token was "
            f"provided (session_update_action={data.get('session_update_action')!r})"
        )
    return {
        "access": access,
        "refresh": data.get("refresh_token"),
        "pat": data.get("privy_access_token"),
    }
