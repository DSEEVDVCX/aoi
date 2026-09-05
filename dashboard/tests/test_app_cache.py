"""The dashboard's heavy routes: computed once per period, and their freshness is declared.

The measured background: `/api/networks` was eating 2.2 seconds of the 2.2
seconds of every dashboard refresh, and the cause was a node scanning three
million rows to produce five. The database is untouchable (no index, no
write), so the fix lives in the code: a result cached in process memory, with
a live freshness stamp on top that costs 0.4ms.

Here we test what's visible from outside: that the computation doesn't
repeat, that the payload states its age, and that "last market" stays live on
top of the stale count **without corrupting the cached rows**.

The spawner is injected in every test: without it the background refresh
thread reads `config.DB_PATH` after `monkeypatch` has restored the real
database — meaning a test that opens the 15GB database behind your back and
then fails in a way nobody can understand.
"""
import sqlite3

import app as dashboard_app
import cache
import config
import dao
import pytest
from fastapi.testclient import TestClient

HOST = {"Host": "127.0.0.1:8090"}

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE watchlist (
  token_address TEXT, network_id TEXT, first_seen_at TEXT, source TEXT,
  watch_until TEXT, entry_signal_id TEXT, active INTEGER,
  is_control INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE market_ticks (
  token_address TEXT, network_id TEXT, recorded_at TEXT, source TEXT,
  price_usd REAL, holders INTEGER, change_24h REAL, volume_24h REAL,
  buy_count_24h INTEGER, sell_count_24h INTEGER
);
"""


class Spawner:
    """Holds the background refresh until the test decides to run it."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)

    def run_all(self) -> int:
        queued, self.pending = self.pending, []
        for run in queued:
            run()
        return len(queued)


def test_cache_evicts_old_cold_entries_when_bounded():
    memo = cache.TTLMemo(max_entries=2)

    memo.get("first", 60, lambda: "one")
    memo.get("second", 60, lambda: "two")
    memo.get("third", 60, lambda: "three")

    assert list(memo._states) == ["second", "third"]


@pytest.fixture()
def spawner(monkeypatch):
    spawn = Spawner()
    monkeypatch.setattr(dashboard_app.cache, "MEMO", cache.TTLMemo(spawn=spawn))
    return spawn


@pytest.fixture()
def db(tmp_path, monkeypatch, spawner):
    """A small database in place of the real one, and a clean temporary cache for every test."""
    path = tmp_path / "rec.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO watchlist VALUES('sol','1399811149','t','large_buy','t2','s',1,0)"
    )
    conn.execute(
        "INSERT INTO market_ticks(token_address, network_id, recorded_at)"
        " VALUES('sol','1399811149','2026-08-18T09:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    return path


def _tick(db, stamp: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO market_ticks(token_address, network_id, recorded_at)"
        " VALUES('sol','1399811149',?)", (stamp,),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(db):
    return TestClient(dashboard_app.app)


def _counted(monkeypatch, name: str) -> list[int]:
    """Wraps a dao function with a counter — how we tell "computed once" from "computed on every request"."""
    calls: list[int] = []
    original = getattr(dao, name)

    def wrapper(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(dao, name, wrapper)
    return calls


# --- the computation doesn't repeat ---
def test_networks_computes_the_heavy_query_once(client, monkeypatch):
    calls = _counted(monkeypatch, "network_summary")

    for _ in range(6):
        assert client.get("/api/networks", headers=HOST).status_code == 200

    assert len(calls) == 1


def test_network_summary_ttl_does_not_rescan_the_archive_aggressively():
    """The summary scans millions of ticks; "last market" is live on top of it, so there's no reason to scan it every two minutes."""
    assert config.NETWORK_SUMMARY_TTL_SECONDS >= 600


def test_labeling_is_cached_like_other_heavy_panels(client, db, monkeypatch):
    """The labeling summary is the heaviest panel after networks (1.2 seconds) — cached the same way.

    And the latest labeling on top of it is live: the labeler's pulse is read
    on every request without waiting for the refresh.
    """
    calls = _counted(monkeypatch, "labeling_outcomes")

    first = client.get("/api/labeling", headers=HOST).json()
    second = client.get("/api/labeling", headers=HOST).json()

    assert len(calls) == 1
    assert first["live"] is False            # no outcomes table in the test database
    assert second == first
    assert first["cache"]["ttl_seconds"] == config.LABELING_TTL_SECONDS


def test_counts_computes_once_within_its_ttl(client, monkeypatch):
    calls = _counted(monkeypatch, "table_counts")

    first = client.get("/api/counts", headers=HOST).json()
    second = client.get("/api/counts", headers=HOST).json()

    assert len(calls) == 1
    assert first["market_ticks"] == second["market_ticks"] == 1


def test_ticks_summary_is_cached_too(client, monkeypatch):
    """The page doesn't call it today, but it costs a second and a half for whoever calls it tomorrow."""
    calls = _counted(monkeypatch, "ticks_summary")

    body = client.get("/api/ticks-summary", headers=HOST).json()
    client.get("/api/ticks-summary", headers=HOST)

    assert len(calls) == 1
    assert body["total"] == 1


def test_watchlist_market_is_cached_like_other_heavy_panels(client, monkeypatch):
    """The watchlist's market view re-aggregates every coin on the page; the
    48-hour countdown stays live via /api/watchlist, this one is cached."""
    calls = _counted(monkeypatch, "watchlist_market")

    first = client.get("/api/watchlist-market", headers=HOST).json()
    second = client.get("/api/watchlist-market", headers=HOST).json()

    assert len(calls) == 1
    assert second == first
    assert first["market"] == {}              # no token_bars in the test database


# --- the payload states its age ---
@pytest.mark.parametrize(
    ("path", "ttl_attr"),
    [
        ("/api/networks", "NETWORK_SUMMARY_TTL_SECONDS"),
        ("/api/counts", "TABLE_COUNTS_TTL_SECONDS"),
        ("/api/ticks-summary", "TICKS_SUMMARY_TTL_SECONDS"),
        ("/api/labeling", "LABELING_TTL_SECONDS"),
        ("/api/watchlist-market", "WATCHLIST_MARKET_TTL_SECONDS"),
    ],
)
def test_cached_routes_publish_their_freshness(client, path, ttl_attr):
    """A number believed to be live when it isn't is worse than a number that states its age."""
    meta = client.get(path, headers=HOST).json()["cache"]

    assert meta["ttl_seconds"] == getattr(config, ttl_attr)
    assert meta["stale"] is False
    assert meta["error"] is None
    assert meta["computed_at"].endswith("+00:00")
    assert meta["age_seconds"] >= 0


# --- live freshness on top of the stale count ---
def test_latest_tick_stays_live_while_counts_are_cached(client, db):
    """The heart of the design: the count is cached, and the pulse is read on every request."""
    first = client.get("/api/networks", headers=HOST).json()["networks"][0]
    assert (first["tick_rows"], first["latest_tick"]) == (1, "2026-08-18T09:00:00+00:00")

    _tick(db, "2026-08-18T11:00:00+00:00")
    second = client.get("/api/networks", headers=HOST).json()["networks"][0]

    assert second["tick_rows"] == 1                            # the count is stale on purpose
    assert second["latest_tick"] == "2026-08-18T11:00:00+00:00"  # and the pulse is live


def test_the_live_overlay_does_not_corrupt_the_cached_rows(client, db, monkeypatch):
    """The cached rows are shared by every request; lifting the stamp inside
    them would have frozen the live one.

    We blind the live lookup after it has lifted the stamp once: if it had
    modified the cache in place, the lifted time would have stayed; the
    correct behavior is for the cached stamp to come back as it was.
    """
    client.get("/api/networks", headers=HOST)
    _tick(db, "2026-08-18T11:00:00+00:00")
    assert client.get("/api/networks", headers=HOST).json()["networks"][0][
        "latest_tick"
    ] == "2026-08-18T11:00:00+00:00"

    monkeypatch.setattr(dao, "latest_tick_per_active_network", lambda conn: {})
    blind = client.get("/api/networks", headers=HOST).json()["networks"][0]
    assert blind["latest_tick"] == "2026-08-18T09:00:00+00:00"


# --- staleness and failure as they appear in the payload ---
def test_stale_payload_is_served_immediately_then_refreshed(client, db, spawner, monkeypatch):
    """Eleven requests from memory and the twelfth waits two seconds — that's what we prevent."""
    monkeypatch.setattr(config, "NETWORK_SUMMARY_TTL_SECONDS", 0.0)
    client.get("/api/networks", headers=HOST)
    _tick(db, "2026-08-18T11:00:00+00:00")

    body = client.get("/api/networks", headers=HOST).json()
    assert body["networks"][0]["tick_rows"] == 1      # the stale one came back without waiting
    assert body["cache"]["stale"] is True
    assert body["cache"]["refreshing"] is True

    assert spawner.run_all() == 1
    assert client.get("/api/networks", headers=HOST).json()["networks"][0]["tick_rows"] == 2


def test_a_failed_refresh_keeps_the_last_good_numbers(client, spawner, monkeypatch):
    """A busy database drops a refresh: we keep the old numbers and say the renewal failed."""
    monkeypatch.setattr(config, "NETWORK_SUMMARY_TTL_SECONDS", 0.0)
    client.get("/api/networks", headers=HOST)

    def boom(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dao, "network_summary", boom)
    client.get("/api/networks", headers=HOST)
    spawner.run_all()

    body = client.get("/api/networks", headers=HOST).json()
    assert body["networks"][0]["tick_rows"] == 1
    assert "database is locked" in body["cache"]["error"]


# --- what the page itself does ---
def test_the_page_asks_for_the_heavy_panels_less_often(client):
    """The server caches 60–600 seconds, so asking it every ten brings the
    same number six times over.

    And the last payload is reused in between — without that the dashboard
    would be empty for a whole minute.
    """
    page = client.get("/", headers=HOST).text

    assert "const HEAVY_EVERY = 6;" in page
    assert "heavyTick % HEAVY_EVERY === 0" in page
    # the second guard: a counter alone would have kept the dashboard empty
    # for a minute after a failed fetch.
    assert "heavy.counts === null" in page
    assert 'getJSON("/api/counts")' in page
    # and the labeling panel too: its 5-minute lifetime is longer than the
    # networks cycle, but it rides the same rhythm — no new request except in
    # a "heavy" cycle.
    assert 'getOptionalJSON("/api/labeling", heavy.labeling' in page
    assert 'paint("labeling", () => renderLabeling(heavy.labeling))' in page


def test_the_heavy_panels_do_not_hold_the_light_ones(client):
    """The load-time contract, measured 2026-09-02.

    Cold — after a restart, or after the nightly backup has read all 26.6 GB
    and evicted the OS page cache — `table_counts` and `network_summary` take
    30 to 60 seconds, because they physically read millions of rows. While
    they were inside the page's single `Promise.all`, the *whole* dashboard
    waited on them: twelve panels had their data in under a second and still
    showed skeletons for a minute, which is exactly what "the dashboard takes
    a long time to load" meant.

    So the heavy half is fetched on its own promise and deliberately not
    awaited. This test pins the two halves apart; without it a later edit that
    slips an `await` back in front of `tickHeavy()` restores the stall
    silently, with every other test still green.
    """
    page = client.get("/", headers=HOST).text

    # fired, never awaited — the comment is part of the contract for the reader
    assert "if (wantHeavy) tickHeavy();" in page
    assert "await tickHeavy()" not in page
    # and the guard that stops slow cycles from stacking on a 60-second compute
    assert "if (heavyInflight) return;" in page

    # the light Promise.all must not carry a heavy path: those four are the
    # ones that scan the archive. (Anchored on the light destructuring itself —
    # `tickHeavy` has a Promise.all of its own, earlier in the file.)
    light = page.split("controlProgress, keys, acct]")[1].split("]);")[0]
    for heavy_path in ("/api/counts", "/api/networks", "/api/labeling", "/api/watchlist-market"):
        assert heavy_path not in light, f"{heavy_path} is back inside the blocking fetch"


def test_counts_ttl_is_longer_than_the_scan_it_pays_for(client):
    """A TTL shorter than its own refresh makes the scan continuous.

    At 60 s the refresh took about as long as the lifetime it was refreshing
    for, so the dashboard process re-scanned the archive roughly half of every
    minute, forever — competing with the recorder for the same disk.
    """
    assert config.TABLE_COUNTS_TTL_SECONDS >= 600


def test_the_page_says_which_numbers_are_cached_and_which_are_live(client):
    """A number believed to be live when it isn't is worse than a number that states its age."""
    page = client.get("/", headers=HOST).text

    assert "renderNetworks(heavy.networks?.networks || [], heavy.networks?.cache)" in page
    assert "Coverage counts computed" in page
    assert '"last market" is live on every refresh' in page
    assert "network-note" in page


def test_a_cold_failure_is_not_hidden_behind_stale_numbers(client, monkeypatch):
    """No previous value ⇒ the error shows. Silencing it with a zero would have read as "no networks"."""
    monkeypatch.setattr(
        dao, "network_summary",
        lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        client.get("/api/networks", headers=HOST)


# --- the line that isn't negotiated ---
def test_caching_writes_nothing_to_the_database(client, db):
    """No cache table and no stamp in `meta`: the database stays as we found it.

    The real guard is `mode=ro`, but this one catches what's subtler: a path
    that thinks it's reading while it stamps something — and any second
    writer competes with the recorder for a lock whose death we've watched
    for three hours once.
    """
    before = sorted(
        sqlite3.connect(db)
        .execute("SELECT name FROM sqlite_master ORDER BY name")
        .fetchall()
    )
    for path in ("/api/networks", "/api/counts", "/api/ticks-summary", "/api/labeling"):
        client.get(path, headers=HOST)

    conn = sqlite3.connect(db)
    after = sorted(conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall())
    assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
    conn.close()
    assert after == before
