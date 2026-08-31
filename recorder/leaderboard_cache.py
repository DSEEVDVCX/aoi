"""Cache of the leaderboard for matching topTraders[].id → rank.

The leaderboard is loaded once an hour (LEADERBOARD_REFRESH_SECONDS) and an
id→rank map is kept in memory. The recorder consults it to compute
top_trader_match_count and buyers_best_rank for each buy event — to catch the
"more than one leaderboard trader bought" signal.

**Four periods, not one**: the source caps `/v2/leaderboard` at 50 traders and
silently ignores every pagination form (measured: nine forms, byte-identical
lists). But the `/24h`, `/7d` and `/30d` paths each return **100** (measured
on archived raw), so the union is 214 traders — four times the coverage
(measured on 7,200 buy events: 3.68% → 15.26%). We keep a **separate map per
period**: rank 7 in `24h` is not rank 7 in `totalPnL`, and merging them into
one number mixes two measurements. `lookup` stays the "all" map alone to
preserve the old contract.

**Raw is retained (`raw_by_period`)**: the leaderboard used to be read and
thrown away every hour, so every trader's path (climbing from #80 to #5 then
burning out) was lost forever — no way to rebuild it retroactively. The
recorder archives each period's raw hourly in snapshots under an independent
source (`leaderboard` / `leaderboard_24h` …). That is why the cache fetches
raw via `_get` directly rather than via the projecting `get_leaderboard`:
projection drops fields (a fact documented in `_map_trader` itself), and only
the raw archive guarantees re-derivation.

Failure survival: one period failing does not erase its old map, does not fail
the other periods, and does not fail the cycle. `refresh` returns True if
**at least one period** succeeded. And the distinction between "loaded" and
"fresh" is essential: `raw_by_period` (for archiving) and `failed_periods`
(for alerting) measure the last attempt, not what lingers in memory —
otherwise an old envelope gets archived under a new timestamp and a persistent
failure stays silent behind a map that succeeded once.
"""
from __future__ import annotations

import asyncio
from typing import Any

from extract import build_rank_lookup, leaderboard_items

# Period order and the base path. "all" = totalPnL (the un-suffixed path).
_ALL = "all"


class LeaderboardCache:
    def __init__(
        self,
        client: Any,
        size: int,
        refresh_seconds: int,
        periods: tuple[str, ...] = (_ALL,),
        pacing_seconds: float = 0.0,
        sleep=asyncio.sleep,
    ) -> None:
        self._client = client
        self._size = size
        self._refresh_seconds = refresh_seconds
        self._periods = tuple(periods) or (_ALL,)
        self._pacing = pacing_seconds
        self._sleep = sleep
        self._lookups: dict[str, dict[str, int]] = {p: {} for p in self._periods}
        self._raw: dict[str, Any] = {}
        self._fresh: tuple[str, ...] = ()
        self._loaded_at_mono: float | None = None

    # --- state reads ---
    @property
    def lookup(self) -> dict[str, int]:
        """The base period's map (totalPnL). Empty before the first successful load."""
        return self._lookups.get(_ALL, {})

    @property
    def lookups(self) -> dict[str, dict[str, int]]:
        """All periods' maps {period → {trader_id → rank}} — never merged."""
        return self._lookups

    @property
    def last_raw(self) -> Any | None:
        """The base period's raw (for archiving). None before the first successful load."""
        return self._raw.get(_ALL)

    @property
    def raw_by_period(self) -> dict[str, Any]:
        """Raw of the periods that succeeded **in the last refresh** — not everything ever loaded.

        `_raw` keeps the last successful envelope per period forever (on
        purpose: reads after a partial failure remain possible), but archiving
        from it would lie: an hour where `30d` fails would re-stamp the
        previous hour's envelope with this hour's timestamp, putting into the
        archive a leaderboard we never fetched. We return only the fresh.
        """
        return {p: self._raw[p] for p in self._fresh if p in self._raw}

    @property
    def raw_seen_by_period(self) -> dict[str, Any]:
        """The last successful raw per period however old — for reading, not archiving."""
        return self._raw

    def set_client(self, client: Any) -> None:
        """Points the cache at a new client (after token rotation). The old maps
        stay valid until the next load — the token changes, not the
        leaderboard's content."""
        self._client = client

    def is_stale(self, now_mono: float) -> bool:
        if self._loaded_at_mono is None:
            return True
        return (now_mono - self._loaded_at_mono) >= self._refresh_seconds

    # --- loading ---
    def _path(self, period: str) -> str:
        from fomo_api.config import settings

        if period == _ALL:
            return settings.upstream_leaderboard_path
        return settings.upstream_leaderboard_period_paths.get(
            period, settings.upstream_leaderboard_path
        )

    async def _refresh_period(self, period: str) -> bool:
        """Loads one period. Failure is local: this period's map and raw stay."""
        try:
            data = await self._client._get(self._path(period), {"limit": self._size})
        except Exception:  # noqa: BLE001 — local failure: the loop above logs it and moves on
            return False  # the loop above records the failure in meta and moves on
        traders = leaderboard_items(data)
        if not traders:
            return False
        # Rank = position (1-based) as in _map_leaderboard — the raw has no rank field.
        new_lookup = build_rank_lookup(
            [{**t, "rank": i + 1} for i, t in enumerate(traders)]
        )
        if not new_lookup:
            return False
        self._lookups[period] = new_lookup
        self._raw[period] = data
        return True

    async def refresh(self, now_mono: float) -> bool:
        """Reloads all periods from raw. True if at least one succeeds.

        Partial failure is acceptable and intended: a broken period does not
        take down the rest, and the stamp is updated on any success so we do
        not enter a retry-every-minute loop over a dead period.
        """
        succeeded = 0
        fresh: list[str] = []
        for i, period in enumerate(self._periods):
            if i and self._pacing:
                await self._sleep(self._pacing)
            if await self._refresh_period(period):
                succeeded += 1
                fresh.append(period)
        self._fresh = tuple(fresh)
        if not succeeded:
            return False
        self._loaded_at_mono = now_mono
        return True

    async def maybe_refresh(self, now_mono: float) -> bool:
        if self.is_stale(now_mono):
            return await self.refresh(now_mono)
        return False

    def failed_periods(self) -> tuple[str, ...]:
        """The periods that failed **the last** attempt — surfaced in meta so the failure is not silent.

        Not "periods without a map": a period that succeeded yesterday and
        fails today keeps its old map (on purpose), so if failure were measured
        by an empty map, the failure would stay silent forever while we keep
        matching with rotten ranks — exactly what this function exists to
        prevent.
        """
        return tuple(p for p in self._periods if p not in self._fresh)
