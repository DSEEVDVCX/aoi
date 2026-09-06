from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from fomo_api.api.errors import UpstreamChangedError, UpstreamUnavailableError
from fomo_api.config import settings

logger = logging.getLogger(__name__)

_TRADER_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_-]+$")
# The global alert feed's confirmed buy variants. `swap_buy` belongs to the
# separate activity backfill stream and is not accepted here until the alert
# endpoint is observed returning it; unknown types must fail closed.
_QUALIFYING_ALERT_TYPES = frozenset({"buy", "large_buy", "multi_user_buy"})
# Stricter than _TRADER_ID_RE on purpose: the batch endpoint rejects the WHOLE
# call when one element is not a uuid, so what goes into a batch is filtered by
# the upstream rule itself ("userId must be a uuid"), not by our looser one.
_UUID_RE = __import__("re").compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HANDLE_RE = __import__("re").compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _make_client(session_token: str) -> Any:
    """curl_cffi in production (bypasses Cloudflare); httpx in dev/test."""
    _headers = {
        "authorization": f"Bearer {session_token}",
        "content-type": "application/json",
        "x-supported-chains": settings.upstream_supported_chains,
        "origin": "https://fomo.family",
        "referer": "https://fomo.family/",
    }
    if settings.upstream_impersonate:
        try:
            from curl_cffi.requests import AsyncSession
            return AsyncSession(
                impersonate="chrome124",
                headers=_headers,
                timeout=settings.upstream_request_timeout_seconds,
            )
        except ImportError:
            pass
    import httpx
    return httpx.AsyncClient(
        base_url=settings.upstream_base.rstrip("/"),
        timeout=settings.upstream_request_timeout_seconds,
        headers=_headers,
    )


class FomoClient:
    """Single adapter to fomo.family's reverse-engineered data API
    (https://prod-api.fomo.family). All upstream access goes through this class
    so undocumented endpoint changes are contained here (constitution Principle
    V, plan.md Complexity Tracking). The session token passed in is the
    consumer's Privy access token, sent as `Authorization: Bearer <token>`
    (CONFIRMED from fomoFetch: `Authorization:`Bearer ${i}`` where `i` is
    Privy's getAccessToken result). Never fabricates data (FR-007): missing/
    unknown fields are None, shape mismatches raise UpstreamChangedError.
    """

    def __init__(self, session_token: str, http_client: Any | None = None) -> None:
        self._token = session_token
        self._base = settings.upstream_base.rstrip("/")
        self._client = http_client or _make_client(session_token)
        self._owns_client = http_client is None
        # httpx clients carry a non-empty base_url and join relative paths
        # themselves; curl_cffi's base_url is empty, so we must pass full URLs.
        _base_url = str(getattr(self._client, "base_url", "") or "")
        self._prepend_base = not _base_url
        self._last_refreshed: datetime | None = None

    def _url(self, path: str) -> str:
        return f"{self._base}{path}" if self._prepend_base else path

    @property
    def last_refreshed_at(self) -> datetime:
        return self._last_refreshed or datetime.now(UTC)

    async def aclose(self) -> None:
        if not self._owns_client:
            return
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close:
            import contextlib
            with contextlib.suppress(Exception):
                await close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._client.get(self._url(path), params=params)
        except Exception as exc:
            raise UpstreamUnavailableError(details={"reason": str(exc)}) from exc
        if resp.status_code >= 500:
            raise UpstreamUnavailableError(details={"upstream_status": resp.status_code})
        if resp.status_code == 401:
            from fomo_api.api.errors import UnauthorizedError
            raise UnauthorizedError("fomo session expired or invalid")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise UpstreamUnavailableError(details={"upstream_status": resp.status_code})
        self._last_refreshed = datetime.now(UTC)
        try:
            return resp.json()
        except Exception as exc:
            raise UpstreamChangedError() from exc

    async def _post(self, path: str, json_body: Any) -> Any:
        """POST a JSON body to an upstream /proxy/* (or similar) read endpoint.

        Mirrors _get's contract: 404 -> None, 401 -> UnauthorizedError,
        5xx/4xx -> UpstreamUnavailableError, un-parseable JSON -> UpstreamChanged.
        These POST routes are DATA READS (screeners, candles, warnings); no
        account/trade state is written (FR-012)."""
        try:
            resp = await self._client.post(self._url(path), json=json_body)
        except Exception as exc:
            raise UpstreamUnavailableError(details={"reason": str(exc)}) from exc
        if resp.status_code >= 500:
            raise UpstreamUnavailableError(details={"upstream_status": resp.status_code})
        if resp.status_code == 401:
            from fomo_api.api.errors import UnauthorizedError
            raise UnauthorizedError("fomo session expired or invalid")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise UpstreamUnavailableError(details={"upstream_status": resp.status_code})
        self._last_refreshed = datetime.now(UTC)
        try:
            return resp.json()
        except Exception as exc:
            raise UpstreamChangedError() from exc

    async def get_leaderboard(
        self, page: int, page_size: int, period: str = "all"
    ) -> dict[str, Any]:
        """CONFIRMED: /v2/leaderboard (all-time) and /v2/leaderboard/{24h,7d,30d}.

        Upstream returns responseObject.leaderboard[] and accepts only a `limit`
        query param (no server-side paging), so we fetch enough rows to cover the
        requested page and let the service slice locally. `rank` is derived from
        array position since upstream has no rank field.
        """
        path = settings.upstream_leaderboard_period_paths.get(
            period, settings.upstream_leaderboard_path
        )
        limit = max(page * page_size, page_size)
        data = await self._get(path, {"limit": limit})
        if data is None:
            raise UpstreamUnavailableError("leaderboard upstream returned no data")
        mapped = _map_leaderboard(data)
        if mapped is None:
            raise UpstreamChangedError()
        all_traders = mapped["traders"]
        total = len(all_traders)
        start = (max(1, page) - 1) * max(1, page_size)
        return {"traders": all_traders[start : start + max(1, page_size)], "total_items": total}

    async def get_trader_profiles(
        self, trader_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """CONFIRMED 2026-08-20: GET /v2/users?userIds=<uuid>&userIds=<uuid> -> 200
        {"responseObject": {"users": [...]}}, the same user objects the old
        per-id path returned.

        This replaces `/v2/users/{id}`, which answers 404 "User not found" for
        every well-formed uuid since ~2026-08-19T14:53Z — proven not to be our
        ids and not our account: ids taken straight out of a 200 from
        `/v2/leaderboard` 404 here too.

        Three measured rules the caller depends on:
          * **Repeated params only.** `userIds=a,b` and a JSON array both 400.
          * **At most 100 per call** (upstream states the cap in the error), so
            longer lists are split across calls.
          * **A malformed id 400s the whole batch**, while an unknown but
            well-formed one is silently omitted from `users`. So ids are filtered
            to real uuids first — one junk id must not cost the other 99 — and
            absence from the reply is the answer "no such user", not an error.

        Returns {trader_id: mapped profile} holding only the ids that came back.
        """
        wanted = [t for t in dict.fromkeys(trader_ids) if _UUID_RE.match(t or "")]
        out: dict[str, dict[str, Any]] = {}
        cap = max(1, settings.upstream_traders_batch_max)
        for start in range(0, len(wanted), cap):
            chunk = wanted[start : start + cap]
            data = await self._get(
                settings.upstream_traders_batch_path, {"userIds": chunk}
            )
            if data is None:
                continue
            # Explicitly, NOT via `_unwrap_list`: that helper returns [] both for
            # "users is empty" and for "there is no users key", and those two must
            # not be one answer here. An empty list is legitimate (every id
            # unknown); a missing key means the shape moved and the honest reply is
            # to raise. Collapsing them is what let `/v2/users/{id}` report "no
            # such trader" for 21 hours instead of "this endpoint is gone".
            users = _unwrap_obj(data).get("users")
            if not isinstance(users, list):
                raise UpstreamChangedError()
            for raw in users:
                mapped = _map_trader(raw)
                if mapped is None:
                    raise UpstreamChangedError()
                out[str(mapped["id"])] = mapped
        return out

    async def get_trader_profile(self, trader_id: str) -> dict[str, Any] | None:
        """One trader, via the batch path — see `get_trader_profiles`.

        A batch of one, deliberately: the old dedicated path is dead upstream, and
        keeping one code path for both means the next upstream change is found and
        fixed in one place instead of two.
        """
        _validate_trader_id(trader_id)
        found = await self.get_trader_profiles([trader_id])
        # Upstream keys the reply by its own `id`. We asked for exactly one, so a
        # non-empty reply IS that trader even if the two strings differ in case.
        return next(iter(found.values()), None)

    async def get_trader_profile_by_handle(self, handle: str) -> dict[str, Any] | None:
        """CONFIRMED: /v2/users/userHandle/{handle} -> same user object shape."""
        if not _HANDLE_RE.match(handle):
            from fomo_api.api.errors import VALIDATION_ERROR, ApiError

            raise ApiError(VALIDATION_ERROR, f"Invalid handle: {handle}", {"handle": handle})
        data = await self._get(settings.upstream_trader_by_handle_path.format(handle=handle))
        if data is None:
            return None
        mapped = _map_trader(_unwrap_obj(data))
        if mapped is None:
            raise UpstreamChangedError()
        return mapped

    async def get_trader_activity(
        self,
        trader_id: str,
        page: int,
        page_size: int,
        from_ts: str | None = None,
        to_ts: str | None = None,
        chain: str | None = None,
        token_id: str | None = None,
    ) -> dict[str, Any] | None:
        """CONFIRMED: /v2/users/{id}/swaps -> responseObject.swaps[].

        Upstream supports only an optional `tokenAddress` filter and no server-side
        paging or time range, so from/to/chain filtering and page slicing are
        applied locally here over the full swap list. `total_items` reflects the
        filtered count (before paging) so the caller can build page metadata."""
        _validate_trader_id(trader_id)
        params: dict[str, Any] = {}
        if token_id:
            params["tokenAddress"] = token_id
        data = await self._get(
            settings.upstream_activity_path.format(trader_id=trader_id), params or None
        )
        if data is None:
            return None
        mapped = _map_activity(data, trader_id)
        if mapped is None:
            raise UpstreamChangedError()
        actions = mapped["actions"]
        if chain:
            actions = [a for a in actions if a.get("chain") == chain]
        from_dt = _parse_timestamp(from_ts)
        to_dt = _parse_timestamp(to_ts)
        if from_ts:
            actions = [
                a for a in actions
                if (timestamp := _parse_timestamp(a.get("timestamp"))) is not None
                and from_dt is not None
                and timestamp >= from_dt
            ]
        if to_ts:
            actions = [
                a for a in actions
                if (timestamp := _parse_timestamp(a.get("timestamp"))) is not None
                and to_dt is not None
                and timestamp <= to_dt
            ]
        total = len(actions)
        start = (max(1, page) - 1) * max(1, page_size)
        actions = actions[start : start + max(1, page_size)]
        return {"actions": actions, "total_items": total}

    async def get_trader_trades(self, trader_id: str, order_by: str | None = None) -> dict[str, Any]:
        """CONFIRMED: /trades?userId= -> responseObject.{activeTrades,closedTrades,...}."""
        _validate_trader_id(trader_id)
        params: dict[str, Any] = {"userId": trader_id}
        if order_by:
            params["orderBy"] = order_by
        data = await self._get(settings.upstream_trades_path, params)
        ro = _unwrap_obj(data) if data is not None else {}
        return {
            "active_trades": ro.get("activeTrades") or [],
            "closed_trades": ro.get("closedTrades") or [],
            "closed_count": _pick(ro, ("closedCount",), cast=int, default=0),
            "has_next_page": bool(ro.get("hasNextPage")),
        }

    async def get_trader_balances(self, trader_id: str) -> Any:
        """CONFIRMED: /v2/users/{id}/balances -> responseObject (holdings)."""
        _validate_trader_id(trader_id)
        data = await self._get(settings.upstream_balances_path.format(trader_id=trader_id))
        if data is None:
            return None
        return _unwrap_obj(data)

    async def get_trader_pnl_snapshot(
        self, trader_id: str, interval: str = "1d", timestamp: int | None = None
    ) -> list[dict[str, Any]]:
        """CONFIRMED: /v2/userTokens/aggregatedSnapshot -> responseObject[] of
        {snapshotId, pnl, equity}. Enriches the profile (which has no PnL)."""
        _validate_trader_id(trader_id)
        params: dict[str, Any] = {"userId": trader_id, "interval": interval}
        if timestamp is not None:
            params["timestamp"] = timestamp
        data = await self._get(settings.upstream_pnl_snapshot_path, params)
        if data is None:
            return []
        raw = _unwrap_list(data, ("snapshots", "data"))
        return [item for item in raw if isinstance(item, dict)]

    async def get_token_thesis_feed(
        self,
        token_address: str,
        network_id: int,
        last_id: str | None = None,
        threshold: int = 1000,
    ) -> list[dict[str, Any]]:
        """CONFIRMED: /feed/token/thesis — thesis/signal feed for a token."""
        params: dict[str, Any] = {"tokenAddress": token_address, "networkId": network_id, "threshold": threshold}
        if last_id:
            params["lastId"] = last_id
        data = await self._get(settings.upstream_feed_token_thesis_path, params)
        if data is None:
            return []
        raw = _unwrap_list(data, ("feed",))
        return [m for m in (_map_thesis_item(i) for i in raw) if m is not None]

    async def get_social_feed(
        self,
        limit: int = 50,
        last_id: str | None = None,
        threshold: int | None = None,
        trader_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """CONFIRMED: /feed/tradingActivity responseObject.items[] — the global
        social feed with post body/likes/views (which the alert mapping drops).
        Optional client-side filter to a single trader's userId."""
        params: dict[str, Any] = {"limit": limit}
        if last_id:
            params["lastId"] = last_id
        if threshold is not None:
            params["threshold"] = threshold
        data = await self._get(settings.upstream_alerts_path, params)
        if data is None:
            return []
        raw = _unwrap_list(data, ("feed", "tradingActivity"))
        posts = [m for m in (_map_feed_post(i) for i in raw) if m is not None]
        if trader_id:
            posts = [p for p in posts if p.get("trader_id") == trader_id]
        return posts

    async def get_trade_comments(self, trade_id: str) -> list[dict[str, Any]]:
        """CONFIRMED: /trades/{id}/comments responseObject.comments[]."""
        _validate_trader_id(trade_id)
        data = await self._get(
            settings.upstream_trade_comments_path.format(trade_id=trade_id)
        )
        if data is None:
            return []
        raw = _unwrap_list(data, ("comments",))
        return [m for m in (_map_comment(i) for i in raw) if m is not None]

    async def get_trader_spotlight(self, trader_id: str) -> dict[str, Any] | None:
        """CONFIRMED: /v2/users/{id}/spotlight responseObject.{bestTrades,bestComments}."""
        _validate_trader_id(trader_id)
        data = await self._get(
            settings.upstream_spotlight_path.format(trader_id=trader_id)
        )
        if data is None:
            return None
        return _map_spotlight(data)

    async def filter_tokens(self, filters: list[str]) -> list[dict[str, Any]]:
        """CONFIRMED: /proxy/filterTokens — filter/screen tokens (body is a JSON array).

        Goes through _post like every other POST read, so an expired session
        raises UnauthorizedError here too (the hand-rolled copy this replaced
        reported a 401 as a generic 502 and never mapped 404 to None)."""
        data = await self._post(settings.upstream_filter_tokens_path, filters)
        if data is None:
            return []
        raw = _unwrap_list(data, ("tokens", "data"))
        return [item for item in raw if isinstance(item, dict)]

    async def get_top_hodlers(self, tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """CONFIRMED: /hodlers/top — top holders for given tokens."""
        import json as _json
        params = {"tokens": _json.dumps(tokens)}
        data = await self._get(settings.upstream_hodlers_top_path, params)
        if data is None:
            return []
        raw = _unwrap_list(data, ("hodlers", "data"))
        return [item for item in raw if isinstance(item, dict)]

    async def get_top_tokens(self, chain: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """CONFIRMED: POST /proxy/trendingTokens -> responseObject[] of trending
        tokens. (The old /tokens/trending path never existed upstream.) Optional
        client-side chain filter by the nested token.networkId."""
        data = await self._post(settings.upstream_trending_tokens_path, {})
        if data is None:
            return []
        raw = _unwrap_list(data, ("tokens", "trendingTokens", "data"))
        mapped = [t for t in (_map_trending_token(t) for t in raw) if t is not None]
        if chain:
            mapped = [t for t in mapped if str(t.get("chain")) == chain]
        return mapped[:limit]

    async def get_trader_positions(self, trader_id: str) -> list[dict[str, Any]]:
        """CONFIRMED: /v2/users/{id}/balances -> responseObject.balances[] where
        each item nests balance / userToken / tokenFilterResult / activeTrade.
        (The old /profile/{id}/positions path never existed upstream.)"""
        _validate_trader_id(trader_id)
        data = await self._get(settings.upstream_balances_path.format(trader_id=trader_id))
        if data is None:
            return []
        raw = _unwrap_list(data, ("balances", "holdings", "tokens", "data"))
        return [p for p in (_map_balance_position(p) for p in raw) if p is not None]

    async def get_token_bars(
        self,
        symbol: str,
        resolution: str = "60",
        from_ts: int | None = None,
        to_ts: int | None = None,
        count_back: int = 300,
        network_id: int | str | None = None,
    ) -> dict[str, Any] | None:
        """CONFIRMED: POST /proxy/getBarsNew -> responseObject {c,h,l,o,t,v,s}
        (TradingView-style OHLCV arrays). This is the historical price series —
        the ground-truth for labelling and paper trading. Read-only (FR-012).

        `symbol` MUST be `"<address>:<networkId>"` (CONFIRMED live 2026-07-26).
        A bare address makes fomo's backend fail with a Cloudflare 502 — which
        looks like an upstream outage but is really a malformed request. Pass
        `network_id` and we build the pair, or pass an already-joined symbol."""
        body: dict[str, Any] = {
            "symbol": _pair_id(symbol, network_id),
            "resolution": resolution,
            "countBack": count_back,
        }
        if from_ts is not None:
            body["from"] = from_ts
        if to_ts is not None:
            body["to"] = to_ts
        data = await self._post(settings.upstream_get_bars_path, body)
        if data is None:
            return None
        return _map_bars(data, symbol=symbol, resolution=resolution)

    async def get_token_details(
        self, token_id: str, network_id: int | str | None = None
    ) -> dict[str, Any] | None:
        """CONFIRMED: POST /proxy/tokenDetails -> responseObject with buy/sell
        counts, multi-window volume, holders and top-10 concentration.

        `tokenId` MUST be `"<address>:<networkId>"` (CONFIRMED live 2026-07-26);
        a bare address 502s the same way getBarsNew does."""
        pair = _pair_id(token_id, network_id)
        data = await self._post(settings.upstream_token_details_path, {"tokenId": pair})
        if data is None:
            return None
        return _map_token_market_detail(data, token_id=pair)

    async def get_token_warnings(self, address: str, network_id: int) -> dict[str, Any] | None:
        """CONFIRMED: POST /proxy/tokenWarnings {address, networkId} ->
        responseObject {disableBuying, disableSelling, warnings[]} (rug/scam flags)."""
        data = await self._post(
            settings.upstream_token_warnings_path,
            {"address": address, "networkId": network_id},
        )
        if data is None:
            return None
        return _map_token_warnings(data, address=address, network_id=network_id)

    async def get_trending_tokens(self) -> list[dict[str, Any]]:
        """CONFIRMED: POST /proxy/trendingTokens {} -> responseObject[] trending."""
        data = await self._post(settings.upstream_trending_tokens_path, {})
        if data is None:
            return []
        raw = _unwrap_list(data, ("tokens", "trendingTokens", "data"))
        return [t for t in (_map_trending_token(t) for t in raw) if t is not None]

    async def get_most_held(self) -> list[dict[str, Any]]:
        """CONFIRMED: POST /proxy/mostHeld {} -> responseObject[] most-held tokens."""
        data = await self._post(settings.upstream_most_held_path, {})
        if data is None:
            return []
        raw = _unwrap_list(data, ("tokens", "mostHeld", "data"))
        return [t for t in (_map_trending_token(t) for t in raw) if t is not None]

    async def get_verified_tokens(self) -> list[dict[str, Any]]:
        """CONFIRMED: GET /proxy/verifiedTokens -> responseObject[] verified tokens."""
        data = await self._get(settings.upstream_verified_tokens_path)
        if data is None:
            return []
        raw = _unwrap_list(data, ("tokens", "verifiedTokens", "data"))
        return [t for t in (_map_trending_token(t) for t in raw) if t is not None]

    async def get_hodler_friends(self, tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """CONFIRMED: POST /hodlers/friends {tokens:[...]} -> responseObject.tokens[]
        of {tokenAddress, networkId, topHolders, totalHolders} (friends holding)."""
        data = await self._post(settings.upstream_hodlers_friends_path, {"tokens": tokens})
        if data is None:
            return []
        raw = _unwrap_list(data, ("tokens", "friends", "data"))
        return [item for item in raw if isinstance(item, dict)]

    async def get_feed(
        self,
        limit: int = 50,
        last_id: str | None = None,
        feed_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """CONFIRMED: GET /feed?feedTypes=...&limit= -> responseObject.items[] global
        social feed. `feedTypes` is a REQUIRED non-empty array of enum values;
        CONFIRMED members: multi_user_buy, multi_user_sell, large_buy. Unknown
        values are silently dropped upstream (an all-unknown list 400s), so we
        default to the confirmed set."""
        types = feed_types or list(settings.feed_default_types)
        params: dict[str, Any] = {"feedTypes": types, "limit": limit}
        if last_id:
            params["lastId"] = last_id
        data = await self._get(settings.upstream_feed_path, params)
        if data is None:
            return []
        raw = _unwrap_list(data, ("feed", "items", "data"))
        return [m for m in (_map_feed_post(i) for i in raw) if m is not None]

    async def get_token_feed(
        self, token_address: str, network_id: int, limit: int = 50, last_id: str | None = None
    ) -> list[dict[str, Any]]:
        """CONFIRMED: GET /feed/token -> responseObject.items[] per-token feed."""
        params: dict[str, Any] = {
            "tokenAddress": token_address,
            "networkId": network_id,
            "limit": limit,
        }
        if last_id:
            params["lastId"] = last_id
        data = await self._get(settings.upstream_feed_token_path, params)
        if data is None:
            return []
        raw = _unwrap_list(data, ("feed", "items", "data"))
        return [m for m in (_map_feed_post(i) for i in raw) if m is not None]

    async def get_trader_alerts(self, trader_id: str, since_ts: str | None = None) -> list[dict[str, Any]]:
        """CONFIRMED: /feed/tradingActivity -> responseObject.feed[] (global feed).

        There is no per-trader alert endpoint; we pull the global trading-activity
        feed and filter to this trader's userId client-side. `since_ts` filters
        newer-than watermark locally (upstream has no `since` param)."""
        _validate_trader_id(trader_id)
        data = await self._get(settings.upstream_alerts_path)
        if data is None:
            return []
        raw = _unwrap_list(data, ("feed", "tradingActivity"))
        alerts: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            event_type = _pick(item, ("type", "action"), cast=str)
            if event_type not in _QUALIFYING_ALERT_TYPES:
                continue
            mapped = _map_alert(item)
            if mapped is None:
                continue
            if mapped.get("trader_id") != trader_id:
                continue
            if since_ts and mapped.get("timestamp") and mapped["timestamp"] <= since_ts:
                continue
            alerts.append(mapped)
        return alerts


def _dict_or_empty(value: Any) -> dict[str, Any]:
    """Narrow an untyped upstream JSON value to a string-keyed object."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _map_balance_position(d: Any) -> dict[str, Any] | None:
    """Map a /v2/users/{id}/balances item (CONFIRMED 2026-07-25). Each item nests:
      - balance:          {tokenAddress, shiftedBalance, tokenId}
      - userToken:        {averageEntryPriceUsd, currentRealizedPnlUsd, holdingSince,
                           humanAmountRemaining, currentCostBasisUsd, ...}
      - tokenFilterResult:{priceUSD, token:{name,symbol,decimals,...}, marketCap, ...}
    We surface a position keyed on the token address; every field optional (FR-007)."""
    if not isinstance(d, dict):
        return None
    bal = _dict_or_empty(d.get("balance"))
    ut = _dict_or_empty(d.get("userToken"))
    tfr = _dict_or_empty(d.get("tokenFilterResult"))
    tinfo = _dict_or_empty(tfr.get("token"))

    address = (
        _pick(bal, ("tokenAddress",), cast=str)
        or _pick(ut, ("tokenAddress",), cast=str)
        or _pick(tinfo, ("address",), cast=str)
    )
    if address is None:
        return None

    token_amount = _pick(bal, ("shiftedBalance",), cast=float)
    if token_amount is None:
        token_amount = _pick(ut, ("humanAmountRemaining",), cast=float)
    price_usd = _pick(tfr, ("priceUSD",), cast=float)
    amount_usd = None
    if token_amount is not None and price_usd is not None:
        amount_usd = token_amount * price_usd

    return {
        "token": {
            "address": address,
            "network_id": _pick(ut, ("networkId",), cast=str),
            "name": _pick(tinfo, ("name",), cast=str),
            "symbol": _pick(tinfo, ("symbol", "ticker"), cast=str),
            "decimals": _pick(tinfo, ("decimals",), cast=int),
        },
        "amount_usd": amount_usd,
        "token_amount": token_amount,
        "avg_entry_price": _pick(ut, ("averageEntryPriceUsd",), cast=float),
        "current_price_usd": price_usd,
        "realized_pnl_usd": _pick(ut, ("currentRealizedPnlUsd",), cast=float),
        "total_realized_pnl_usd": _pick(ut, ("totalRealizedPnlUsd",), cast=float),
        "cost_basis_usd": _pick(ut, ("currentCostBasisUsd",), cast=float),
        "market_cap_usd": _pick(tfr, ("marketCap",), cast=float),
        "opened_at": _pick(ut, ("holdingSince",), cast=str),
    }


def _map_trending_token(d: Any) -> dict[str, Any] | None:
    """Map a POST /proxy/trendingTokens | /mostHeld | /verifiedTokens item.

    The market fields (change/liquidity/marketCap/priceUSD) sit at the top level
    and the token identity is nested under `token` (address, decimals, networkId,
    name, symbol, isScam, socialLinks, launchpad). Everything optional (FR-007)."""
    if not isinstance(d, dict):
        return None
    tok = _dict_or_empty(d.get("token"))
    address = _pick(tok, ("address",), cast=str) or _pick(d, ("address", "tokenAddress"), cast=str)
    if address is None:
        return None
    return {
        "address": address,
        "name": _pick(tok, ("name",), cast=str),
        "symbol": _pick(tok, ("symbol", "ticker"), cast=str),
        "chain": _pick(tok, ("networkId",), cast=str) or _pick(d, ("networkId",), cast=str),
        "decimals": _pick(tok, ("decimals",), cast=int),
        "is_scam": _pick(tok, ("isScam",), cast=bool),
        "launchpad": _pick(tok, ("launchpad",), cast=str),
        "price_usd": _pick(d, ("priceUSD", "priceUsd", "price"), cast=float),
        "change_pct": _pick(d, ("change",), cast=float),
        "liquidity_usd": _pick(d, ("liquidity",), cast=float),
        "market_cap_usd": _pick(d, ("marketCap", "fdv"), cast=float),
        "volume_usd": _pick(d, ("volume", "volume24h"), cast=float),
    }


def _map_bars(data: Any, *, symbol: str, resolution: str) -> dict[str, Any] | None:
    """Map POST /proxy/getBarsNew responseObject {c,h,l,o,t,v,s} into an OHLCV
    series. `s` is TradingView's status string ("ok"/"no_data"). We surface the
    parallel arrays verbatim (no fabrication) so a missing array becomes []."""
    ro = _unwrap_obj(data)
    if not isinstance(ro, dict):
        raise UpstreamChangedError()

    def _floats(key: str) -> list[float]:
        v = ro.get(key)
        if not isinstance(v, list):
            return []
        out: list[float] = []
        for x in v:
            try:
                out.append(float(x))
            except (TypeError, ValueError):
                continue
        return out

    def _ints(key: str) -> list[int]:
        v = ro.get(key)
        if not isinstance(v, list):
            return []
        out: list[int] = []
        for x in v:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    return {
        "symbol": symbol,
        "resolution": resolution,
        "status": _pick(ro, ("s",), cast=str),
        "t": _ints("t"),
        "o": _floats("o"),
        "h": _floats("h"),
        "l": _floats("l"),
        "c": _floats("c"),
        "v": _floats("v"),
    }


def _map_token_market_detail(data: Any, *, token_id: str) -> dict[str, Any] | None:
    """Map POST /proxy/tokenDetails responseObject: trade counts, multi-window
    volume (5m/1h/4h/24h), holders and top-10 concentration. All optional."""
    ro = _unwrap_obj(data)
    if not isinstance(ro, dict):
        raise UpstreamChangedError()
    vol = _dict_or_empty(ro.get("volume"))
    return {
        "token_id": token_id,
        "buy_count": _pick(ro, ("buyCount",), cast=int),
        "sell_count": _pick(ro, ("sellCount",), cast=int),
        "holders": _pick(ro, ("holders",), cast=int),
        "top10_holders_pct": _pick(ro, ("top10HoldersPercent",), cast=float),
        "is_low_fees": _pick(ro, ("isLowFees",), cast=bool),
        "volume_5m_usd": _pick(vol, ("5m", "m5"), cast=float),
        "volume_1h_usd": _pick(vol, ("1", "1h", "h1"), cast=float),
        "volume_4h_usd": _pick(vol, ("4", "4h", "h4"), cast=float),
        "volume_24h_usd": _pick(vol, ("24", "24h", "h24"), cast=float),
    }


def _map_token_warnings(data: Any, *, address: str, network_id: int) -> dict[str, Any] | None:
    """Map POST /proxy/tokenWarnings responseObject: trade gates + warning list."""
    ro = _unwrap_obj(data)
    if not isinstance(ro, dict):
        raise UpstreamChangedError()
    warnings = ro.get("warnings")
    return {
        "address": address,
        "network_id": str(network_id),
        "disable_buying": _pick(ro, ("disableBuying",), cast=bool),
        "disable_selling": _pick(ro, ("disableSelling",), cast=bool),
        "warnings": warnings if isinstance(warnings, list) else [],
    }


def _unwrap_obj(data: Any) -> dict[str, Any]:
    """Return the dict payload from fomo.family's response envelope.
    Shapes: {"responseObject": {...}} (the norm) or a bare {...}."""
    if isinstance(data, dict):
        ro = data.get("responseObject")
        if isinstance(ro, dict):
            return ro
        return data
    return {}


def _unwrap_list(data: Any, keys: tuple[str, ...]) -> list[Any]:
    """Extract a list from fomo.family's response envelope.
    Shapes seen: bare list; {"responseObject": [...]}; {"responseObject": {"items":[...]}};
    {"items":[...]}; {"data":[...]}; {<key>:[...]}.
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    ro = data.get("responseObject")
    if isinstance(ro, list):
        return ro
    if isinstance(ro, dict):
        for k in ("items", "data", *keys):
            v = ro.get(k)
            if isinstance(v, list):
                return v
    for k in ("items", "data", *keys):
        v = data.get(k)
        if isinstance(v, list):
            return v
    return []


def _first_list(d: dict[str, Any], keys: tuple[str, ...]) -> list[Any] | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, list):
            return v
    return None


def _pick(d: dict[str, Any], keys: tuple[str, ...], *, cast: type, default: Any = None) -> Any:
    """Presence-based field picker — preserves falsy values like 0.0."""
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return cast(d[k])
            except (TypeError, ValueError):
                return default
    return default


def _pair_id(address: str, network_id: int | str | None) -> str:
    """Build fomo's `"<address>:<networkId>"` token identifier.

    Returns `address` unchanged when it already carries a `:networkId` suffix or
    when no network id is available — so callers that already joined the pair
    (and legacy callers that cannot supply one) keep working."""
    if network_id is None or ":" in address:
        return address
    return f"{address}:{network_id}"


def _validate_trader_id(trader_id: str) -> str:
    if not _TRADER_ID_RE.match(trader_id):
        from fomo_api.api.errors import VALIDATION_ERROR, ApiError

        raise ApiError(VALIDATION_ERROR, f"Invalid trader_id: {trader_id}", {"trader_id": trader_id})
    return trader_id


def _map_metrics(d: dict[str, Any]) -> dict[str, Any]:
    """Map the leaderboard/profile user object to TraderMetrics fields.

    Real fields (CONFIRMED 2026-07-25): totalVolume, numTrades, swapCount, and a
    single period PnL — totalPnL (all-time) or pnl24h/pnl7d/pnl30d depending on
    the leaderboard variant. There is no win-rate or avg-trade-size upstream, so
    those stay None (FR-007: no fabrication)."""
    pnl = _pick(
        d, ("totalPnL", "pnl24h", "pnl7d", "pnl30d", "pnl"), cast=float
    )
    return {
        "pnl_pct": None,
        "win_rate_pct": None,
        "volume_usd": _pick(d, ("totalVolume", "volume_usd", "volumeUsd"), cast=float),
        "avg_trade_size_usd": None,
        "trade_count": _pick(d, ("numTrades", "swapCount", "trade_count"), cast=int),
        "realized_pnl_usd": pnl,
    }


def _map_trader(d: Any) -> dict[str, Any] | None:
    """Map a fomo user object (leaderboard row or /v2/users/{id}) to a Trader.

    CONFIRMED fields: id, userHandle, displayName, followers, address/evmAddress,
    totalVolume, numTrades. `rank` is injected by _map_leaderboard from array
    position (upstream has no rank field); a bare profile has no rank."""
    if not isinstance(d, dict):
        return None
    tid = _pick(d, ("id", "trader_id", "traderId"), cast=str)
    handle = _pick(d, ("userHandle", "handle", "displayName"), cast=str)
    if tid is None or handle is None:
        return None
    rank = _pick(d, ("rank",), cast=int)  # injected by leaderboard; None on profile
    followers = _pick(d, ("followers", "followers_count", "followerCount"), cast=int, default=0)
    metrics = _map_metrics(d)
    return {
        "id": tid,
        "handle": handle,
        "display_name": _pick(d, ("displayName",), cast=str),
        "rank": rank,
        "followers_count": followers,
        "num_trades": _pick(d, ("numTrades", "swapCount"), cast=int),
        "total_volume_usd": _pick(d, ("totalVolume",), cast=float),
        "metrics": metrics,
        "wallet_address": _pick(d, ("evmAddress", "address", "wallet_address"), cast=str),
        "chains": [],
        # Social/profile fields (CONFIRMED upstream) — previously dropped.
        "twitter": _pick(d, ("twitter",), cast=str),
        "description": _pick(d, ("description", "bio"), cast=str),
        "avg_hold_time_seconds": _pick(d, ("averageHoldTimeSeconds",), cast=int),
        "following": _pick(d, ("following",), cast=int),
        "created_at": _pick(d, ("createdAt",), cast=str),
        "profile_picture_url": _pick(d, ("profilePictureLink", "profilePicture"), cast=str),
        "is_private": _pick(d, ("private",), cast=bool),
    }


def _map_leaderboard(d: Any) -> dict[str, Any] | None:
    """CONFIRMED: responseObject.leaderboard[] — a list of user objects with no
    rank field. Rank is derived from position (1-based)."""
    ro = _unwrap_obj(d)
    traders_raw = _first_list(ro, ("leaderboard", "traders", "data", "items"))
    if traders_raw is None:
        return None
    traders: list[dict[str, Any]] = []
    for i, t in enumerate(traders_raw):
        if isinstance(t, dict):
            t = {**t, "rank": i + 1}
        mapped = _map_trader(t)
        if mapped is not None:
            traders.append(mapped)
    return {"traders": traders, "total_items": len(traders)}


def _map_activity(d: Any, trader_id: str = "") -> dict[str, Any] | None:
    """CONFIRMED: responseObject.swaps[] — each is a token->token swap.

    We surface the OUT token (what the trader acquired) as the activity token,
    action="swap", amount_usd from humanUsdAmountOut. `chain` = str(networkId),
    tx_hash = swap id (no on-chain hash is exposed). Missing symbols are left
    None — the SPA resolves symbols from token metadata we don't fetch here."""
    ro = _unwrap_obj(d)
    swaps_raw = _first_list(ro, ("swaps", "actions", "data", "items"))
    if swaps_raw is None:
        return None
    actions: list[dict[str, Any]] = []
    for item in swaps_raw:
        if not isinstance(item, dict):
            continue
        aid = _pick(item, ("id", "action_id", "actionId"), cast=str)
        ts = _pick(item, ("createdAt", "timestamp", "time"), cast=str)
        network = _pick(item, ("networkId", "outNetworkId", "inNetworkId"), cast=str)
        if aid is None or ts is None or network is None:
            raise UpstreamChangedError()
        out_addr = _pick(item, ("outTokenAddress",), cast=str)
        token = {
            "id": out_addr or aid,
            "symbol": None,
            "chain": network,
            "address": out_addr,
            "price_usd": None,
        }
        actions.append(
            {
                "id": aid,
                "trader_id": trader_id or _pick(item, ("userId", "trader_id"), cast=str, default=""),
                "action": "swap",
                "token": token,
                "amount_usd": _pick(item, ("humanUsdAmountOut", "humanUsdAmountIn"), cast=float),
                "token_amount": _pick(item, ("outHumanAmount", "outAmount"), cast=float),
                "price_usd": None,
                "chain": network,
                "timestamp": ts,
                "tx_hash": aid,
            }
        )
    return {"actions": actions, "total_items": len(actions)}


def _map_alert(d: Any) -> dict[str, Any] | None:
    """CONFIRMED: an item from /feed/tradingActivity responseObject.feed[].
    Fields: id, userId, tradeId, swapId, transferId, tokenAddress, networkId,
    createdAt, type. Maps to the alert shape (trader_id from userId)."""
    if not isinstance(d, dict):
        return None
    aid = _pick(d, ("id", "alert_id", "alertId"), cast=str)
    trader_id = _pick(d, ("userId", "trader_id", "traderId"), cast=str)
    ts = _pick(d, ("createdAt", "timestamp", "time"), cast=str)
    network = _pick(d, ("networkId",), cast=str)
    token_addr = _pick(d, ("tokenAddress",), cast=str)
    if aid is None or trader_id is None or ts is None:
        return None
    token = {
        "id": token_addr or aid,
        "symbol": None,
        "chain": network or "",
        "address": token_addr,
        "price_usd": None,
    }
    return {
        "id": aid,
        "trader_id": trader_id,
        "token": token,
        "amount_usd": None,
        "timestamp": ts,
    }


def _map_feed_post(d: Any) -> dict[str, Any] | None:
    """Map an item from /feed/tradingActivity responseObject.items[] to a
    FeedPost, PRESERVING the social payload (body/likes/views/pinned) that
    _map_alert drops. CONFIRMED fields: id, userId, tradeId, tokenAddress,
    networkId, createdAt, type, body, likes, views, pinned."""
    if not isinstance(d, dict):
        return None
    pid = _pick(d, ("id",), cast=str)
    if pid is None:
        return None
    body = d.get("body")
    if not isinstance(body, dict):
        body = None  # body is a structured payload or absent; never a scalar
    return {
        "id": pid,
        "trader_id": _pick(d, ("userId", "trader_id", "traderId"), cast=str),
        "type": _pick(d, ("type",), cast=str),
        "body": body,
        "likes": _pick(d, ("likes",), cast=int),
        "views": _pick(d, ("views",), cast=int),
        "pinned": _pick(d, ("pinned",), cast=bool),
        "token_address": _pick(d, ("tokenAddress",), cast=str),
        "network_id": _pick(d, ("networkId",), cast=str),
        "trade_id": _pick(d, ("tradeId",), cast=str),
        "timestamp": _pick(d, ("createdAt", "timestamp"), cast=str),
    }


def _map_thesis_item(d: Any) -> dict[str, Any] | None:
    """Map an item from /feed/token/thesis responseObject.items[] — a trader's
    thesis tweet on a coin. CONFIRMED (live probe 2026-07-25): the top-level
    item carries type, id, tradeId, ticker, tokenAddress, networkId, userHandle,
    displayName, numReplies, equity, createdAt; and a nested `comment` OBJECT
    whose `.comment` is the thesis text (and `.numLikes` the like count)."""
    if not isinstance(d, dict):
        return None
    tid = _pick(d, ("id",), cast=str)
    if tid is None:
        return None
    comment_obj = _dict_or_empty(d.get("comment"))
    # Fallback: some shapes may carry a plain-string comment inline.
    text = _pick(comment_obj, ("comment",), cast=str)
    if text is None and isinstance(d.get("comment"), str):
        text = d["comment"]
    return {
        "id": tid,
        "type": _pick(d, ("type",), cast=str),
        "comment": text,
        "num_likes": _pick(comment_obj, ("numLikes",), cast=int),
        "ticker": _pick(d, ("ticker",), cast=str),
        "token_address": _pick(d, ("tokenAddress",), cast=str),
        "network_id": _pick(d, ("networkId",), cast=str),
        "user_handle": _pick(d, ("userHandle",), cast=str),
        "display_name": _pick(d, ("displayName",), cast=str),
        "profile_picture_url": _pick(d, ("profilePictureLink",), cast=str),
        "num_replies": _pick(d, ("numReplies",), cast=int),
        "equity": _pick(d, ("equity",), cast=float),
        "trade_id": _pick(d, ("tradeId",), cast=str),
        "created_at": _pick(d, ("createdAt",), cast=str),
    }


def _map_comment(d: Any) -> dict[str, Any] | None:
    """Map an item from /trades/{id}/comments responseObject.comments[].
    CONFIRMED fields: id, userId, tradeId, comment, createdAt, parentId,
    numLikes, tokenAddress, networkId."""
    if not isinstance(d, dict):
        return None
    cid = _pick(d, ("id",), cast=str)
    if cid is None:
        return None
    return {
        "id": cid,
        "trader_id": _pick(d, ("userId", "trader_id"), cast=str),
        "comment": _pick(d, ("comment",), cast=str),
        "num_likes": _pick(d, ("numLikes", "likes"), cast=int),
        "parent_id": _pick(d, ("parentId",), cast=str),
        "token_address": _pick(d, ("tokenAddress",), cast=str),
        "network_id": _pick(d, ("networkId",), cast=str),
        "trade_id": _pick(d, ("tradeId",), cast=str),
        "created_at": _pick(d, ("createdAt",), cast=str),
    }


def _map_spotlight_item(d: Any) -> dict[str, Any] | None:
    """Map one entry of spotlight.bestTrades[]/bestComments[]. CONFIRMED shape
    (live probe 2026-07-25): each entry has displayName, userHandle,
    profilePictureLink, and a nested `comment` OBJECT (with .comment text,
    .numLikes, .tokenAddress, .createdAt) and a nested `trade` object (with
    .id, .realizedPnlUsd). We extract the text and identifiers so no data is
    lost and nothing is fabricated."""
    if not isinstance(d, dict):
        return None
    trade = _dict_or_empty(d.get("trade"))
    comment_obj = _dict_or_empty(d.get("comment"))
    # trade id: prefer the trade object's id, then a top-level tradeId/id.
    trade_id = _pick(trade, ("id",), cast=str) or _pick(d, ("tradeId", "id"), cast=str)
    return {
        "user_handle": _pick(d, ("userHandle",), cast=str),
        "display_name": _pick(d, ("displayName",), cast=str),
        "profile_picture_url": _pick(d, ("profilePictureLink",), cast=str),
        "comment": _pick(comment_obj, ("comment",), cast=str),
        "num_likes": _pick(comment_obj, ("numLikes", "likes"), cast=int),
        "token_address": _pick(comment_obj, ("tokenAddress",), cast=str)
        or _pick(trade, ("tokenAddress",), cast=str),
        "realized_pnl_usd": _pick(trade, ("realizedPnlUsd",), cast=float),
        "trade_id": trade_id,
        "created_at": _pick(comment_obj, ("createdAt",), cast=str)
        or _pick(trade, ("createdAt",), cast=str),
    }


def _map_spotlight(d: Any) -> dict[str, Any]:
    """Map /v2/users/{id}/spotlight responseObject -> {best_trades, best_comments}."""
    ro = _unwrap_obj(d)
    best_trades = [
        m for m in (_map_spotlight_item(i) for i in (ro.get("bestTrades") or [])) if m is not None
    ]
    best_comments = [
        m for m in (_map_spotlight_item(i) for i in (ro.get("bestComments") or [])) if m is not None
    ]
    return {"best_trades": best_trades, "best_comments": best_comments}
