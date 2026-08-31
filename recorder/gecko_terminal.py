"""Generic GeckoTerminal client — token-age fallback via pool_created_at.

Why this layer exists: the age gate fails closed on "unknown" when
`filterTokens` cannot find the token (non-trending tokens in particular —
measured: the unknown class is not the young class). Measured 2026-08-27 on a
sample of fomo unknowns: **17 of 18** came back from GeckoTerminal with a real
age via the oldest pool. A plain HTTP client is blocked (403 on any
non-browser UA), so we use curl_cffi with a Chrome fingerprint, same as the
fomo client itself.

Design constraints, all measured:
- The anonymous limit is ~30 calls/min with no key → one call per token, the
  cycle carries only new candidates (median 1.11/cycle), and the result is
  stored so it is never asked again.
- The list is sorted by size, not time → take the **oldest** `pool_created_at`
  from the first page (a token may open newer pools later; the old one is the
  birth).
- 429 means "later", not "failed": it returns None and fomo continues its
  normal path; the retry is governed by `AGE_MISSING_RETRY_SECONDS` in
  `token_age_lookup_state`.

Safety: `pool_created_at` is the birth of the first pool ever — which is what
the gate measures ("tradable age"); a live comparison on a shared token showed
an 88-second difference from fomo's `createdAt`, i.e. agreement within a minute
over a two-day gate range.
"""
from __future__ import annotations

import asyncio
from typing import Any

# Map of our network ids to GeckoTerminal network ids (measured in the field 2026-08-27).
NETWORK_MAP = {
    "1399811149": "solana",
    "4663": "eth",
    "8453": "base",
    "143": "arbitrum",
    "56": "bsc",
}

_BASE = "https://api.geckoterminal.com/api/v2/networks"
_TIMEOUT = 15


class GeckoTerminalClient:
    """Read-only client for pool_created_at. Created once and reused."""

    def __init__(self, session: Any = None) -> None:
        self._session = session

    async def _get(self, url: str) -> Any:
        if self._session is None:
            from curl_cffi.requests import AsyncSession

            self._session = AsyncSession(
                impersonate="chrome124",
                headers={"accept": "application/json"},
                timeout=_TIMEOUT,
            )
        return await self._session.get(url)

    async def pool_created_at(self, address: str, network_id: str) -> str | None:
        """The oldest pool_created_at for the token, or None if absent/rate-limited.

        Never raises: network failure, rate limit and absence are all None —
        the layer above tells them apart via the stored lookup state, and this
        call is just a fallback.
        """
        gecko_net = NETWORK_MAP.get(str(network_id))
        if not gecko_net:
            return None
        url = f"{_BASE}/{gecko_net}/tokens/{address}/pools"
        try:
            resp = await self._get(url)
        except Exception:  # noqa: BLE001 — a fallback must never kill the cycle
            return None
        if getattr(resp, "status_code", None) != 200:
            return None            # 404 not found, 429 rate limit — later
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 — unexpected response
            return None
        created: list[str] = [
            pool["attributes"]["pool_created_at"]
            for pool in data.get("data", [])
            if isinstance(pool, dict)
            and isinstance(pool.get("attributes"), dict)
            and pool["attributes"].get("pool_created_at")
        ]
        return min(created) if created else None

    async def aclose(self) -> None:
        if self._session is not None:
            close = getattr(self._session, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — a close whose failure does not matter
                    pass
            self._session = None


_shared: GeckoTerminalClient | None = None
_shared_lock = asyncio.Lock()


async def shared_gecko_client() -> GeckoTerminalClient:
    """One process-wide client — the session is reused, not recreated."""
    global _shared
    if _shared is None:
        async with _shared_lock:
            if _shared is None:
                _shared = GeckoTerminalClient()
    return _shared
