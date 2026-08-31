"""Tests of the leaderboard cache after its move to raw (no network).

Locks in the contract the archival is built on: rank is derived from position
(the raw has no rank field), and the full raw envelope is kept in `last_raw`
for the recorder to archive hourly in snapshots — without it every trader's
path is lost forever. Failure keeps the old map and old raw (it does not wipe
them).
"""

from leaderboard_cache import LeaderboardCache

import recorder

RAW = {
    "success": True,
    "responseObject": {
        "leaderboard": [
            {"id": "trader_A", "userHandle": "alice", "totalPnL": 999.0},
            {"id": "trader_B", "userHandle": "bob"},
            {"userHandle": "no_id"},          # no id → not in the map
            {"id": "trader_C", "userHandle": "carol"},
        ]
    },
}


class _RawClient:
    """Fake client that returns a prepared raw envelope or raises on demand."""

    def __init__(self, data=RAW, boom=False):
        self._data = data
        self._boom = boom
        self.calls = []

    async def _get(self, path, params=None):
        self.calls.append((path, params))
        if self._boom:
            raise RuntimeError("upstream boom")
        return self._data


async def test_refresh_builds_lookup_from_raw_position():
    lb = LeaderboardCache(_RawClient(), size=200, refresh_seconds=3600)
    assert await lb.refresh(now_mono=100.0) is True
    # Rank = 1-based position (as in _map_leaderboard): C is 4th despite the
    # empty third
    assert lb.lookup == {"trader_A": 1, "trader_B": 2, "trader_C": 4}


async def test_refresh_keeps_full_raw_for_archival():
    lb = LeaderboardCache(_RawClient(), size=200, refresh_seconds=3600)
    await lb.refresh(now_mono=100.0)
    assert lb.last_raw is RAW   # the whole envelope, not a projected list


async def test_refresh_passes_configured_limit():
    client = _RawClient()
    lb = LeaderboardCache(client, size=200, refresh_seconds=3600)
    await lb.refresh(now_mono=100.0)
    assert client.calls[0][1] == {"limit": 200}


async def test_failure_keeps_old_lookup_and_raw():
    lb = LeaderboardCache(_RawClient(), size=200, refresh_seconds=3600)
    await lb.refresh(now_mono=100.0)
    old_lookup, old_raw = dict(lb.lookup), lb.last_raw

    lb.set_client(_RawClient(boom=True))
    assert await lb.refresh(now_mono=4000.0) is False
    assert lb.lookup == old_lookup        # the old map stayed
    assert lb.last_raw is old_raw         # the old raw stayed


async def test_malformed_envelope_returns_false_and_keeps_state():
    lb = LeaderboardCache(_RawClient(data={"responseObject": {}}), 200, 3600)
    assert await lb.refresh(now_mono=100.0) is False
    assert lb.lookup == {}
    assert lb.last_raw is None


async def test_none_envelope_returns_false():
    lb = LeaderboardCache(_RawClient(data=None), 200, 3600)
    assert await lb.refresh(now_mono=100.0) is False


async def test_staleness_drives_maybe_refresh():
    client = _RawClient()
    lb = LeaderboardCache(client, size=200, refresh_seconds=3600)
    assert await lb.maybe_refresh(now_mono=100.0) is True    # first time: always stale
    assert await lb.maybe_refresh(now_mono=200.0) is False   # fresh — no call
    assert len(client.calls) == 1
    assert await lb.maybe_refresh(now_mono=100.0 + 3601) is True  # time to refresh
    assert len(client.calls) == 2


# --- the four periods: 50 per leaderboard, the union multiplies coverage ---
def _raw(ids):
    return {"responseObject": {"leaderboard": [{"id": i} for i in ids]}}


class _PeriodClient:
    """A client returning a different list per path (and optionally failing on a
    given path)."""

    def __init__(self, by_path, boom_paths=()):
        self._by_path = by_path
        self._boom = set(boom_paths)
        self.calls = []

    async def _get(self, path, params=None):
        self.calls.append((path, params))
        if path in self._boom:
            raise RuntimeError("upstream boom")
        return self._by_path.get(path, {"responseObject": {}})


_PATHS = {
    "/v2/leaderboard": _raw(["A", "B"]),
    "/v2/leaderboard/24h": _raw(["B", "C"]),
    "/v2/leaderboard/7d": _raw(["D"]),
    "/v2/leaderboard/30d": _raw(["E"]),
}
_PERIODS = ("all", "24h", "7d", "30d")


async def test_four_periods_kept_separate():
    """Each period gets its own map: B's rank is 2 in totalPnL and 1 in 24h —
    no merging."""
    lb = LeaderboardCache(
        _PeriodClient(_PATHS), 200, 3600, periods=_PERIODS
    )
    assert await lb.refresh(now_mono=100.0) is True
    assert lb.lookups["all"] == {"A": 1, "B": 2}
    assert lb.lookups["24h"] == {"B": 1, "C": 2}
    assert lb.lookups["7d"] == {"D": 1}
    assert lb.lookups["30d"] == {"E": 1}
    # `lookup` stays the base period (the old contract is preserved)
    assert lb.lookup == {"A": 1, "B": 2}


async def test_union_widens_coverage_beyond_the_fifty():
    """The point of the expansion: the union matches traders the base
    leaderboard does not know."""
    lb = LeaderboardCache(_PeriodClient(_PATHS), 200, 3600, periods=_PERIODS)
    await lb.refresh(now_mono=100.0)
    union = {t for lut in lb.lookups.values() for t in lut}
    assert union == {"A", "B", "C", "D", "E"}
    assert len(union) > len(lb.lookup)


async def test_one_failing_period_does_not_sink_the_rest():
    client = _PeriodClient(_PATHS, boom_paths=["/v2/leaderboard/7d"])
    lb = LeaderboardCache(client, 200, 3600, periods=_PERIODS)
    assert await lb.refresh(now_mono=100.0) is True   # partial success = success
    assert lb.lookups["24h"] == {"B": 1, "C": 2}
    assert lb.lookups["7d"] == {}                    # empty, not fabricated
    assert lb.failed_periods() == ("7d",)
    assert not lb.is_stale(200.0)                    # the stamp updated, so no retry loop


async def test_all_periods_failing_returns_false():
    client = _PeriodClient(_PATHS, boom_paths=list(_PATHS))
    lb = LeaderboardCache(client, 200, 3600, periods=_PERIODS)
    assert await lb.refresh(now_mono=100.0) is False
    assert lb.is_stale(200.0)                        # nothing loaded ⇒ retry


async def test_failed_period_keeps_previous_lookup():
    """A period that succeeded then failed: its old map stays — not wiped —
    but the failure is reported."""
    lb = LeaderboardCache(_PeriodClient(_PATHS), 200, 3600, periods=_PERIODS)
    await lb.refresh(now_mono=100.0)
    lb.set_client(_PeriodClient(_PATHS, boom_paths=["/v2/leaderboard/24h"]))
    await lb.refresh(now_mono=4000.0)
    assert lb.lookups["24h"] == {"B": 1, "C": 2}      # we still match with it, so not wiped
    assert lb.failed_periods() == ("24h",)            # but it is rotten: not silent


async def test_stale_raw_is_not_rearchived_under_a_new_timestamp():
    """An hour where a period fails must not re-stamp its old envelope —
    otherwise the archive holds a leaderboard we never fetched at that time."""
    lb = LeaderboardCache(_PeriodClient(_PATHS), 200, 3600, periods=_PERIODS)
    await lb.refresh(now_mono=100.0)
    assert set(lb.raw_by_period) == set(_PERIODS)
    lb.set_client(_PeriodClient(_PATHS, boom_paths=["/v2/leaderboard/24h"]))
    await lb.refresh(now_mono=4000.0)
    assert "24h" not in lb.raw_by_period               # the fake fresh is not archived
    assert lb.raw_seen_by_period["24h"] is _PATHS["/v2/leaderboard/24h"]


async def test_each_period_archived_under_its_own_source():
    lb = LeaderboardCache(_PeriodClient(_PATHS), 200, 3600, periods=_PERIODS)
    await lb.refresh(now_mono=100.0)
    assert set(lb.raw_by_period) == set(_PERIODS)
    assert lb.raw_by_period["24h"] is _PATHS["/v2/leaderboard/24h"]
    assert lb.last_raw is _PATHS["/v2/leaderboard"]


async def test_periods_are_paced():
    """A pause between period calls — kindness to the source: three naps for
    four periods."""
    naps: list[float] = []

    async def _sleep(sec):
        naps.append(sec)

    lb = LeaderboardCache(
        _PeriodClient(_PATHS), 200, 3600, periods=_PERIODS,
        pacing_seconds=1.5, sleep=_sleep,
    )
    await lb.refresh(now_mono=100.0)
    assert naps == [1.5, 1.5, 1.5]


async def test_single_period_default_is_backward_compatible():
    """The old call form (no periods) stays on the base period alone."""
    client = _PeriodClient(_PATHS)
    lb = LeaderboardCache(client, 200, 3600)
    await lb.refresh(now_mono=100.0)
    assert [c[0] for c in client.calls] == ["/v2/leaderboard"]
    assert lb.lookups == {"all": {"A": 1, "B": 2}}


# --- refresh_leaderboard: raw archival + failure recording (cycle level) ---
class _StubLB:
    """A fake cache with a pre-set state — no network."""

    def __init__(self, stale, refreshed, raw=None, raw_by_period=None, failed=()):
        self._stale = stale
        self._refreshed = refreshed
        self.last_raw = raw
        self.raw_by_period = (
            raw_by_period if raw_by_period is not None
            else ({"all": raw} if raw is not None else {})
        )
        self._failed = tuple(failed)

    def is_stale(self, now_mono):
        return self._stale

    async def maybe_refresh(self, now_mono):
        return self._refreshed

    def failed_periods(self):
        return self._failed


class _StubDB:
    def __init__(self):
        self.meta: dict[str, str] = {}
        self.snapshots: list[tuple[str, object]] = []

    def set_meta(self, k, v):
        self.meta[k] = v

    def insert_snapshot(self, source, raw, recorded_at):
        self.snapshots.append((source, raw))


async def test_refresh_leaderboard_archives_raw_on_success():
    db = _StubDB()
    lb = _StubLB(stale=True, refreshed=True, raw={"responseObject": {"leaderboard": []}})
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert db.snapshots == [("leaderboard", {"responseObject": {"leaderboard": []}})]
    assert "last_error_leaderboard" not in db.meta


async def test_refresh_leaderboard_failure_is_recorded_not_silent():
    """A due refresh that failed ⇒ the map stays old — it must be visible in
    meta, not silent."""
    db = _StubDB()
    lb = _StubLB(stale=True, refreshed=False)
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert db.snapshots == []
    assert "last_error_leaderboard" in db.meta


async def test_refresh_leaderboard_fresh_cache_does_nothing():
    db = _StubDB()
    lb = _StubLB(stale=False, refreshed=False)
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert db.snapshots == [] and db.meta == {}


async def test_refresh_leaderboard_archives_each_period_separately():
    """Each period's raw under its own source — merging into one source mixes
    four lists."""
    db = _StubDB()
    lb = _StubLB(
        stale=True, refreshed=True,
        raw_by_period={"all": {"a": 1}, "24h": {"b": 2}, "7d": {"c": 3}},
    )
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert [s[0] for s in db.snapshots] == [
        "leaderboard", "leaderboard_24h", "leaderboard_7d"
    ]
    assert "last_error_leaderboard" not in db.meta


async def test_refresh_leaderboard_partial_failure_is_visible():
    """Partial success does not fail the cycle, but it is not silent either."""
    db = _StubDB()
    lb = _StubLB(
        stale=True, refreshed=True, raw_by_period={"all": {"a": 1}}, failed=("30d",)
    )
    await recorder.refresh_leaderboard(lb, db, 100.0, "t")
    assert db.snapshots == [("leaderboard", {"a": 1})]
    assert "30d" in db.meta["last_error_leaderboard"]
