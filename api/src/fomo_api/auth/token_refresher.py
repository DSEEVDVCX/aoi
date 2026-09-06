from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PRIVY_TOKEN_URL = "https://auth.privy.io/api/v1/sessions"
_REFRESH_BEFORE_EXPIRY = 300  # renew 5 min before expiry
_POLL_INTERVAL = 60


def _expiry_of(token: str | None) -> float | None:
    """The `exp` claim inside a JWT, or None when unreadable.

    Same no-verification rule as `_did_of`: this is our own bookkeeping about a
    token we already hold, never an authorization decision.
    """
    if not token:
        return None
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(body)).get("exp")
    except Exception:  # a token we cannot parse has no usable expiry
        return None
    return float(exp) if isinstance(exp, (int, float)) else None


def _did_of(token: str | None) -> str | None:
    """The Privy DID (`sub`) inside a JWT, or None when unreadable.

    Signature is NOT verified and must not be: this is used only to notice that
    the identity on disk differs from the one in Redis, never to authorize.
    """
    if not token:
        return None
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        sub = json.loads(base64.urlsafe_b64decode(body)).get("sub")
    except Exception:  # a token we cannot parse simply has no comparable identity
        return None
    return str(sub) if sub else None


class TokenRefresher:
    """Background task that renews the Privy access token using the stored
    refresh token, then updates all active sessions in Redis."""

    def __init__(self, session_store: Any, cred_store: Any | None = None) -> None:
        self._store = session_store
        # Optional CredentialStore: when present, every successful renewal is
        # also mirrored to disk so the ROTATED refresh_token + pat survive
        # restarts and the extractor keeps running with zero manual logins.
        self._cred_store = cred_store
        self._task: asyncio.Task[None] | None = None
        # DID this loop is currently renewing; see _adopt_disk_identity.
        self._identity: str | None = None

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

    async def _adopt_disk_identity(
        self, creds: dict[str, str | None]
    ) -> dict[str, str | None]:
        """Adopt the credential file when it holds a DIFFERENT Privy identity.

        Redis is seeded from disk at boot and then kept in step by every
        renewal, so normally the two agree and this is a no-op. But an account
        can be switched out of band — the dashboard writes the credential file
        directly — and this loop would otherwise keep renewing the OLD identity
        and mirror it straight back over the new one, silently undoing the
        switch within a poll interval.

        The comparison is by DID, not by refresh_token, on purpose. Privy
        rotates the refresh_token on every renewal, so "disk differs" is the
        normal state for a moment after each write and, if a disk write ever
        failed, disk would hold a spent token — adopting that would break
        renewal outright. A different DID cannot happen by rotation; it only
        happens when a human changed the account.

        The identity we compare against is the one THIS loop last renewed, kept
        in memory. Redis holds no single "current access token" to read back —
        `update_access_tokens` fans a new token out across every active session
        — so the refresher's own record is the only honest reference point. On
        the first pass it is simply seeded from disk, which is also where
        bootstrap seeded Redis from, so nothing is adopted spuriously at boot.
        """
        if self._cred_store is None:
            return creds
        try:
            stored = self._cred_store.load()
        except Exception as exc:  # an unreadable file must not stop renewal
            logger.warning("Could not read credential file: %s", exc)
            return creds
        if stored is None or not stored.refresh_token:
            return creds
        disk_did = _did_of(stored.access_token)
        if not disk_did:
            return creds
        if self._identity is None:
            self._identity = disk_did
            return creds
        if disk_did == self._identity:
            return creds
        logger.info(
            "Credential file holds a different Privy identity (%s -> %s); "
            "adopting it for renewal",
            self._identity,
            disk_did,
        )
        self._identity = disk_did
        await self._store.update_access_tokens(
            stored.access_token, stored.refresh_token, stored.pat
        )
        adopted = dict(creds)
        adopted.update(
            refresh_token=stored.refresh_token,
            pat=stored.pat or creds.get("pat"),
            app_id=stored.app_id or creds.get("app_id"),
            client_id=stored.client_id or creds.get("client_id"),
            ca_id=stored.ca_id or creds.get("ca_id"),
        )
        return adopted

    async def _maybe_refresh(self) -> None:
        creds = await self._store.get_full_refresh_creds()
        if not creds:
            return
        creds = await self._adopt_disk_identity(creds)
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
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            logger.warning("No Privy refresh token stored; cannot refresh token")
            return
        result = await _call_privy_refresh(
            refresh_token=refresh_token,
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
    # The comment above says `token: null` means "yours is still valid". That is
    # Privy's intent but NOT a guarantee, and trusting it cost an hour of silent
    # collection loss on 2026-08-20: Privy answered 200 + `ignore` every 60s while
    # the retained token sat expired, and this function returned it, and the loop
    # logged "refreshed successfully" — so nothing anywhere said the word wrong
    # while the recorder took 401 on all 38 calls a cycle. A renewal that hands
    # back an EXPIRED token has failed no matter what the envelope says, and it
    # must say so: raising here leaves the spent token unpersisted, lets `_loop`
    # log it, and retries on the next poll.
    if data.get("token") is None:
        exp = _expiry_of(access)
        if exp is not None and exp <= time.time():
            raise RuntimeError(
                "Privy declined to mint a token "
                f"(session_update_action={data.get('session_update_action')!r}) and "
                f"the retained one expired {int(time.time() - exp)}s ago — this "
                "session can no longer renew; a fresh login is required"
            )
    return {
        "access": access,
        "refresh": data.get("refresh_token"),
        "pat": data.get("privy_access_token"),
    }
