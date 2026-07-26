from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class PrivyLoginError(Exception):
    pass


@dataclass
class PrivyCredentials:
    access_token: str
    refresh_token: str | None
    app_id: str | None
    # Extra fields required to renew the token via Privy's API without a browser.
    # CONFIRMED (2026-07-25) that POST auth.privy.io/api/v1/sessions needs:
    #   Authorization: Bearer <pat>  (privy:pat, att="pat")
    #   headers privy-app-id / privy-client / privy-client-id / privy-ca-id
    pat: str | None = None
    client_id: str | None = None
    ca_id: str | None = None


class PrivyLoginService:
    """Drives fomo.family's Privy OAuth/wallet-social login via a headless
    browser and harvests the resulting Privy **access token**. The browser is
    used ONLY for the login step; subsequent data reads use lightweight httpx
    calls to the data API (https://prod-api.fomo.family) with the access token
    sent as `Authorization: Bearer <token>` (see fomo_client.py / research.md).

    CONFIRMED flow (static recon of fomo.family SPA, 2026-07-24):
      1. The SPA (https://fomo.family) uses Privy for auth. After a successful
         social/wallet login, Privy stores an access token (JWT) obtainable via
         `getAccessToken`.
      2. fomo's inline boot script redirects to the `/token` route with
         `?privy_oauth_code&privy_oauth_state&privy_oauth_provider` query params,
         which completes the OAuth callback.
      3. The data wrapper (fomoFetch) calls `getAccessToken()` and sends the
         result as `Authorization: Bearer <token>` to https://prod-api.fomo.family.
    So the harvested credential is a Privy access token (not a fomo cookie),
    and it is stored only in volatile memory (FR-013).
    """

    def __init__(
        self,
        upstream_app_base: str,
        browser_factory: Any | None = None,
        *,
        login_timeout_seconds: float = 300.0,
    ) -> None:
        self._upstream_app_base = upstream_app_base.rstrip("/")
        self._browser_factory = browser_factory
        self._login_timeout = login_timeout_seconds

    async def login(self) -> str:
        """Complete a Privy login and return the Privy access token.

        Thin wrapper over login_with_credentials() kept for backward
        compatibility — callers that only need the access token still work.
        """
        creds = await self.login_with_credentials()
        return creds.access_token

    async def login_with_credentials(self) -> PrivyCredentials:
        """Complete a Privy login and return access token + refresh token + app id.

        Drives a real browser: navigates to the SPA, lets the operator complete
        the Privy wallet/social login interactively, then harvests the Privy
        access token from the first authenticated request the SPA sends to the
        data API (CONFIRMED: `Authorization: Bearer <token>`), plus the refresh
        token and app id from localStorage so a background task can renew the
        access token without a browser. Never fabricates (FR-007): if no access
        token is captured, raises PrivyLoginError.
        """
        factory = self._browser_factory
        if factory is None:
            from fomo_api.config import settings

            if not settings.enable_live_login:
                raise PrivyLoginError(
                    "Live Privy login is disabled. Set FOMO_API_ENABLE_LIVE_LOGIN=true "
                    "to drive an interactive browser login, or provide a session token directly."
                )
            from fomo_api.auth.browser import default_browser_factory

            factory = default_browser_factory
        logger.info("Starting Privy login flow against %s", self._upstream_app_base)
        ca_id_holder: dict[str, str] = {}
        async with await factory() as page_ctx:
            self._attach_ca_id_observer(page_ctx.page, ca_id_holder)
            token = await self._harvest_session(page_ctx)
            refresh_token = await self._read_refresh_token(page_ctx.page)
            app_id = await self._read_app_id(page_ctx.page)
            pat = await self._read_pat(page_ctx.page)
            client_id = await self._read_client_id(page_ctx.page)
        if not token:
            raise PrivyLoginError("Privy login completed but no access token was captured")
        if not refresh_token:
            logger.warning("Login succeeded but no Privy refresh token captured; auto-renew disabled")
        return PrivyCredentials(
            access_token=token,
            refresh_token=refresh_token,
            app_id=app_id,
            pat=pat,
            client_id=client_id,
            ca_id=ca_id_holder.get("ca_id"),
        )

    def _attach_ca_id_observer(self, page: Any, holder: dict[str, str]) -> None:
        """Capture the `privy-ca-id` request header the SPA sends to Privy."""

        def _on_request(request: Any) -> None:
            if holder.get("ca_id"):
                return
            if "auth.privy.io" not in request.url:
                return
            ca = request.headers.get("privy-ca-id")
            if ca:
                holder["ca_id"] = ca

        page.on("request", _on_request)

    async def _harvest_session(self, page_ctx: Any) -> str | None:
        """Wait for a COMPLETED Privy user login, then return the access token.

        Privy's SDK emits an anonymous/app-level access token on init (before
        any user login), which the data API rejects with 401. The reliable
        signal that a real user session exists is the presence of
        `privy:refresh_token` in localStorage — it is written only after a
        successful login (CONFIRMED via storage dump 2026-07-25). So we poll
        localStorage until BOTH `privy:token` (a JWT) and `privy:refresh_token`
        are present, and read the access token from `privy:token`.
        """
        import asyncio

        page = page_ctx.page
        await page.goto(self._upstream_app_base, wait_until="domcontentloaded")
        logger.info("Waiting for operator to complete Privy login (timeout %ss)", self._login_timeout)

        read_state = (
            "() => ({"
            "  token: localStorage.getItem('privy:token'),"
            "  refresh: localStorage.getItem('privy:refresh_token')"
            "})"
        )

        deadline_iters = int(self._login_timeout / 1.0)
        for _ in range(max(deadline_iters, 1)):
            try:
                state = await page.evaluate(read_state)
            except Exception:
                await asyncio.sleep(1.0)
                continue
            token = (state or {}).get("token")
            refresh = (state or {}).get("refresh")
            if token and refresh:
                # strip optional surrounding quotes Privy sometimes stores
                return token.strip().strip('"')
            await asyncio.sleep(1.0)

        logger.info("Login not completed within timeout; trying localStorage fallback")
        return await self._read_token_from_storage(page)

    async def _read_refresh_token(self, page: Any) -> str | None:
        script = (
            "() => {"
            "  for (let i = 0; i < localStorage.length; i++) {"
            "    const k = localStorage.key(i);"
            "    if (k && k.toLowerCase().includes('privy') &&"
            "        k.toLowerCase().includes('refresh')) {"
            "      const v = localStorage.getItem(k);"
            "      if (v) return v.replace(/^\"|\"$/g, '');"
            "    }"
            "  }"
            "  return null;"
            "}"
        )
        try:
            token = await page.evaluate(script)
        except Exception as exc:
            logger.warning("localStorage refresh token read failed: %s", exc)
            return None
        return token if isinstance(token, str) and token else None

    async def _read_pat(self, page: Any) -> str | None:
        """Read the Privy PAT (`privy:pat`) used as the Bearer token when
        refreshing a session (CONFIRMED: decoded JWT has att="pat")."""
        try:
            v = await page.evaluate("() => localStorage.getItem('privy:pat')")
        except Exception as exc:
            logger.warning("localStorage pat read failed: %s", exc)
            return None
        return v.strip().strip('"') if isinstance(v, str) and v else None

    async def _read_client_id(self, page: Any) -> str | None:
        """Read the Privy client id from `privy:connections` or any localStorage
        value that carries it; sent as the `privy-client-id` header."""
        script = (
            "() => {"
            "  for (let i = 0; i < localStorage.length; i++) {"
            "    const k = localStorage.key(i);"
            "    const v = localStorage.getItem(k) || '';"
            "    const m = v.match(/client-[A-Za-z0-9]{20,}/);"
            "    if (m) return m[0];"
            "  }"
            "  return null;"
            "}"
        )
        try:
            v = await page.evaluate(script)
        except Exception as exc:
            logger.warning("localStorage client_id read failed: %s", exc)
            return None
        return v if isinstance(v, str) and v else None

    async def _read_app_id(self, page: Any) -> str | None:
        """Extract the Privy app id, which is embedded in localStorage key names
        like `privy:<appId>:recent-login-method` (CONFIRMED via storage dump)."""
        script = (
            "() => {"
            "  const re = /^privy:([a-z0-9]{20,}):/i;"
            "  for (let i = 0; i < localStorage.length; i++) {"
            "    const k = localStorage.key(i);"
            "    const m = k && k.match(re);"
            "    if (m) return m[1];"
            "  }"
            "  return null;"
            "}"
        )
        try:
            app_id = await page.evaluate(script)
        except Exception as exc:
            logger.warning("localStorage app_id read failed: %s", exc)
            return None
        return app_id if isinstance(app_id, str) and app_id else None

    async def _read_token_from_storage(self, page: Any) -> str | None:
        """Fallback: read the Privy access token from browser localStorage."""
        script = (
            "() => {"
            "  for (let i = 0; i < localStorage.length; i++) {"
            "    const k = localStorage.key(i);"
            "    if (k && k.toLowerCase().includes('privy') &&"
            "        (k.includes('access_token') || k.includes('token'))) {"
            "      const v = localStorage.getItem(k);"
            "      if (v) return v.replace(/^\"|\"$/g, '');"
            "    }"
            "  }"
            "  return null;"
            "}"
        )
        try:
            token = await page.evaluate(script)
        except Exception as exc:  # pragma: no cover
            logger.warning("localStorage token read failed: %s", exc)
            return None
        return token if isinstance(token, str) and token else None
