"""Generic DEX Screener client — the socials layer only (the single measured value).

Full inventory 2026-08-29 of every field in their response against what we
already have from fomo: price/liquidity/volume/change/tx count/pool age/
platform/images/websites — **all duplicated or we do better** (our flow is
deeper: 5 minutes with two windows vs a bare m5 count). The only genuinely
added value: `info.socials[]` — measured coverage 11/12 active tokens (92%).

The two columns we build on it:
  1. `social_channels_dex`   — number of social channels DEX knows (marketing
     legitimacy).
  2. `social_match_fomo_dex` — agreement of the two sources: a token fomo
     says has a twitter while DEX sees nothing = the forged-profile pattern
     (a common scam signal), and the reverse is mutual honesty that raises
     trust.

Measured constraints: the rate limit is generous (we never saw a 429 during
measurement) but the call is made only for each **new** token — asked once at
admission and stored in token_static (token constants are written once), so
no continuous loop and no double collection. Tokens DEX does not answer stay
NULL (absence, not zero — it may simply not be indexed yet).

Networks: a map of our ids → theirs (Solana is case-sensitive).
"""
from __future__ import annotations

from typing import Any

# Map of our networks → DEX Screener chainId.
NETWORK_MAP = {
    "1399811149": "solana",
    "4663": "ethereum",
    "8453": "base",
    "56": "bsc",
    "143": "arbitrum",
}

_BASE = "https://api.dexscreener.com/latest/dex/tokens"
_TIMEOUT = 15


class DexScreenerClient:
    """Read-only client for social channels. Created once per cycle that uses it."""

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

    async def social_channels(self, address: str, network_id: str) -> int | None:
        """Number of social channels of the most liquid pair, or None if not indexed.

        Never raises: network failure and absence are both None — the layer
        above stores the absence as-is (a measured absence, not zero).
        """
        chain = NETWORK_MAP.get(str(network_id))
        if not chain:
            return None
        addr = address if str(network_id) == "1399811149" else address.lower()
        try:
            resp = await self._get(f"{_BASE}/{addr}")
        except Exception:  # noqa: BLE001 — a supporting layer must never kill the cycle
            return None
        if getattr(resp, "status_code", None) != 200:
            return None            # not indexed or rate limit — later
        try:
            pairs = resp.json().get("pairs") or []
        except Exception:  # noqa: BLE001
            return None
        if not pairs:
            return None
        # The most liquid pair is the primary pair for the token's overall profile.
        top = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        info = top.get("info") or {}
        socials = info.get("socials")
        return len(socials) if isinstance(socials, list) else None

    async def aclose(self) -> None:
        if self._session is not None:
            close = getattr(self._session, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — a close whose failure does not matter
                    pass
            self._session = None
