"""Recorder token-rotation tests (no network).

Pins that the recorder picks up the renewed token on disk every cycle and
rebuilds the client when it changes — the permanent fix for the hourly
session expiry (the token lives 60 minutes) with no manual login. No
network, no token value printed.
"""
import asyncio
import sqlite3
from typing import ClassVar

import pytest

import recorder


class _FakeClient:
    """A fake client recording the token it was built with and whether it was closed."""

    instances: ClassVar[list["_FakeClient"]] = []

    def __init__(self, token: str) -> None:
        self.token = token
        self.closed = False
        _FakeClient.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


class _FakeCache:
    def __init__(self, client):
        self.client = client
        self.set_client_calls = 0

    def set_client(self, client):
        self.client = client
        self.set_client_calls += 1


class _FakeDB:
    def __init__(self):
        self.meta: dict[str, str] = {}
        self.closed = False
        self.recoveries = 0

    def set_meta(self, k, v):
        self.meta[k] = v

    def note_error(self, k, v):
        """Mimics `RecorderDB.note_error`: writes via set_meta and never raises."""
        try:
            self.set_meta(k, v)
            return True
        except Exception:  # noqa: BLE001
            return False

    def bump_counter(self, k, n=1):
        pass

    def recover_connection(self):
        self.recoveries += 1
        return "ok"

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_instances():
    _FakeClient.instances = []
    yield
    _FakeClient.instances = []


@pytest.fixture()
def patched(monkeypatch):
    """Replaces _build_client with a fake client and silences _log (no log file written)."""
    monkeypatch.setattr(recorder, "_build_client", lambda tok: _FakeClient(tok))
    monkeypatch.setattr(recorder, "_log", lambda msg: None)
    return monkeypatch


def _rotate(client, token, lb, db):
    return asyncio.run(recorder._maybe_rotate_client(client, token, lb, db))


def test_no_rotation_when_token_unchanged(patched, monkeypatch):
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_A")
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is c0                # the same client
    assert token == "TOK_A"
    assert c0.closed is False           # not closed
    assert lb.set_client_calls == 0
    assert "last_token_refresh_at" not in db.meta


def test_rotation_rebuilds_client_and_closes_old(patched, monkeypatch):
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_B")
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is not c0             # a new client
    assert client.token == "TOK_B"      # built with the new token
    assert token == "TOK_B"
    assert c0.closed is True            # the old one was closed
    assert lb.set_client_calls == 1     # the cache was pointed at the new one
    assert lb.client is client
    assert "last_token_refresh_at" in db.meta


def test_disk_read_failure_keeps_current_client(patched, monkeypatch):
    def _boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(recorder, "_load_access_token", _boom)
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is c0                 # we continue with the current one
    assert token == "TOK_A"
    assert c0.closed is False
    assert "last_error_token_reload" in db.meta   # the error was logged (with no secret value)


def test_old_client_close_error_does_not_break_rotation(patched, monkeypatch):
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_B")

    class _BadClose(_FakeClient):
        async def aclose(self):
            raise RuntimeError("close failed")

    c0 = _BadClose("TOK_A")
    lb, db = _FakeCache(c0), _FakeDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client.token == "TOK_B"      # rotation succeeded despite the close failure
    assert token == "TOK_B"
    assert lb.client is client


def test_meta_key_never_contains_token_value(patched, monkeypatch):
    """A security shield: meta values never contain the token — only a timestamp."""
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "SECRET_TOKEN_VALUE")
    c0 = _FakeClient("OLD")
    lb, db = _FakeCache(c0), _FakeDB()
    _rotate(c0, "OLD", lb, db)
    for v in db.meta.values():
        assert "SECRET_TOKEN_VALUE" not in v


# ---------------------------------------------------------------------------
# A locked database must not take down the process (measured 2026-08-17)
#
# `database is locked` in `insert_holders` also raised out of the error
# handler that writes the failure description, then out of
# `bump_counter("cycle_crashes")` in the loop shield itself — the recorder
# exited with code 1 and the task sat `Ready`, silent, for three hours. The
# shield must not die by its own hand.
# ---------------------------------------------------------------------------
class _LockedDB(_FakeDB):
    """Accepts the first `accept` writes, then refuses everything after as a locked database."""

    def __init__(self, *, accept: int = 0):
        super().__init__()
        self.attempts = 0
        self._accept = accept

    def set_meta(self, k, v):
        self.attempts += 1
        if self.attempts > self._accept:
            raise sqlite3.OperationalError("database is locked")
        super().set_meta(k, v)

    def bump_counter(self, k, n=1):
        raise sqlite3.OperationalError("database is locked")


def test_rotation_survives_a_locked_database(patched, monkeypatch):
    """The stamp is bookkeeping, not measurement: a lock must not waste a client actually built with the new token."""
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_B")
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _LockedDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client.token == "TOK_B"                  # rotation happened despite the lock
    assert token == "TOK_B"
    assert lb.client is client
    assert "last_token_refresh_at" not in db.meta   # only the stamp was lost


def test_token_reload_note_survives_a_locked_database(patched, monkeypatch):
    """The read-failure handler used to write to the database bare — its lock used to take down the process."""
    def _boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(recorder, "_load_access_token", _boom)
    c0 = _FakeClient("TOK_A")
    lb, db = _FakeCache(c0), _LockedDB()
    client, token = _rotate(c0, "TOK_A", lb, db)
    assert client is c0                             # we continue with the current one
    assert token == "TOK_A"


def test_locked_database_does_not_end_the_cycle_loop(patched, monkeypatch):
    """The exact regression: three crashing cycles complete the loop without taking down the process."""
    db = _LockedDB(accept=3)          # the three startup stamps pass, then it locks
    ran = []

    async def _crashing_cycle(*_a, **_k):
        ran.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(recorder, "RecorderDB", lambda *a, **k: db)
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_A")
    monkeypatch.setattr(recorder, "LeaderboardCache", lambda client, **k: _FakeCache(client))
    monkeypatch.setattr(recorder, "run_cycle", _crashing_cycle)
    monkeypatch.setattr(recorder.config, "CYCLE_SECONDS", 0)

    asyncio.run(recorder.main_loop(cycles=3))

    assert len(ran) == 3              # all three ran: the shield survived its counter being locked
    assert db.closed is True          # and the loop exited cleanly, not by blowing up
    # And logging alone was not enough: a wedged connection kills every
    # following cycle the way it killed this one. 2026-08-19: thirteen
    # cycles crashing identically, 22 minutes 40 seconds without a single
    # row, until a manual restart.
    assert db.recoveries == 3         # one rescue per crash, not one per loop


# --- The blocking breaker: calibrated on the real 2026-08-19T14:53→16:07 numbers ---
# Cycle counters 20 (clean), 21 (turning), 22 (partial), and 23 (blocked) are
# copied as-is from `recorder.log`, so the threshold is tested against what
# actually happened, not a contrived example.
_LIVE_OK = {"signals": 5, "ticks": 122, "bars_rows": 6328, "social_items": 21134,
            "holders_details": 13, "flow_rows": 13, "filter_ticks": 109,
            "traders_rows": 4, "errors": 0}
_LIVE_MIXED = {"signals": 2, "ticks": 124, "bars_rows": 6250, "filter_ticks": 107,
               "holders_details": 13, "traders_rows": 4, "errors": 3}
_LIVE_PARTIAL = {"holders_details": 1, "holders_top": 2, "flow_rows": 1,
                 "filter_requested": 190, "errors": 49}
_LIVE_BLOCKED = {"filter_requested": 190, "errors": 52}


def test_a_dead_cycle_is_errors_with_no_row_written():
    """`filter_requested` is not yield: it is the only number that **rose** during the block.

    108→190 because the filter is requested for every coin that received no
    snapshot; were it counted as yield, the breaker would stay closed through
    the very block it was built for.
    """
    assert recorder._cycle_is_dead(_LIVE_BLOCKED) is True
    assert recorder._cycle_is_dead(_LIVE_OK) is False        # no errors at all
    assert recorder._cycle_is_dead(_LIVE_MIXED) is False     # errors alongside yield
    assert recorder._cycle_is_dead(_LIVE_PARTIAL) is False   # a single row is enough


def test_the_wait_doubles_to_a_ceiling_and_never_overflows(monkeypatch):
    monkeypatch.setattr(recorder.config, "CYCLE_SECONDS", 60)
    monkeypatch.setattr(recorder.config, "UPSTREAM_BREAKER_AFTER", 3)
    monkeypatch.setattr(recorder.config, "UPSTREAM_BREAKER_MAX_SECONDS", 900)

    waits = [recorder._breaker_wait(k) for k in range(3, 9)]

    assert waits == [60, 120, 240, 480, 900, 900]
    assert recorder._breaker_wait(10_000) == 900   # an exponent that raises no OverflowError


def test_the_probe_answers_false_when_the_source_refuses():
    """The block raises an exception; the probe translates it into an answer, not a crash."""
    class _Blocked:
        async def _get(self, path, params=None):
            raise RuntimeError("403 you have been blocked")

    class _Open:
        async def _get(self, path, params=None):
            return None            # a 404 is an answer from the origin ⇒ the road is open

    assert asyncio.run(recorder._upstream_alive(_Blocked())) is False
    assert asyncio.run(recorder._upstream_alive(_Open())) is True

    # And the reason comes out as text: the wait decision is one, the remedy forks on it.
    why: list[str] = []
    assert asyncio.run(recorder._upstream_alive(_Blocked(), why)) is False
    assert "403" in why[0]
    assert asyncio.run(recorder._upstream_alive(_Open(), why)) is True
    assert len(why) == 1                   # success writes no reason


def test_the_probe_sends_the_required_feed_types_and_reads_400_as_alive():
    """Measured 2026-08-20T00:24Z with sound arithmetic: `/feed?limit=1` alone returns 400.

    `feedTypes` is a required parameter on `/feed`, and `_get` translates
    every ≥400 into "unavailable", so the probe was reading a live road as
    dead — meaning the breaker closes and never reopens even once the block
    is lifted. The log witnessed it: eleven closures with not one resume
    except by restart. Two verdicts: the call carries its required
    parameter, and an application-level answer is by itself life.
    """
    from fomo_api.api.errors import UpstreamUnavailableError

    seen: list[dict] = []

    class _Recording:
        async def _get(self, path, params=None):
            seen.append(dict(params or {}))
            return {"responseObject": {"items": []}}

    assert asyncio.run(recorder._upstream_alive(_Recording())) is True
    assert seen[0].get("feedTypes") == list(recorder.config.FEED_TYPES)
    assert seen[0].get("limit") == 1        # the road's condition, not its payload

    # 400 = the request arrived and was examined ⇒ life. Should the source add a
    # required parameter tomorrow, it must not kill collection.
    class _Status:
        def __init__(self, code): self.code = code
        async def _get(self, path, params=None):
            raise UpstreamUnavailableError(details={"upstream_status": self.code})

    why: list[str] = []
    assert asyncio.run(recorder._upstream_alive(_Status(400), why)) is True
    assert asyncio.run(recorder._upstream_alive(_Status(422), why)) is True
    assert why == []                        # life writes no reason

    # And 403 is an identity block, 5xx an outage — running the cycle fixes neither.
    assert asyncio.run(recorder._upstream_alive(_Status(403), why)) is False
    assert asyncio.run(recorder._upstream_alive(_Status(503), why)) is False
    assert len(why) == 2
    assert "403" in why[0]


def test_the_note_carries_the_status_code_not_the_default_message():
    """Measured 2026-08-19: a 403 on identity was written as "unreachable", so DNS was chased for an hour.

    The default `UpstreamUnavailableError` message is the same for a block
    and for an outage, and what separates them lives in `details` — if that
    is dropped, the distinction is lost from the whole dashboard.
    """
    from fomo_api.api.errors import UpstreamUnavailableError

    blocked = UpstreamUnavailableError(details={"upstream_status": 403})
    note = recorder._exc_note(blocked)
    assert "403" in note                       # the code is present
    assert "UpstreamUnavailableError" in note   # and the name is not lost

    # And a long transport text is trimmed: the note is a row in meta, not a
    # debugging log.
    long = recorder._exc_note(UpstreamUnavailableError(details={"reason": "x" * 400}))
    assert len(long) < 260

    # And an error without details stays as it was — no empty parentheses.
    assert recorder._exc_note(RuntimeError("boom")) == "RuntimeError: boom"


def _loop_with(monkeypatch, cycle_stats, alive, cycles):
    """Runs main_loop with a cycle returning given counters and a controlled probe.

    Returns (number of collecting cycles, number of probe calls, the fake database).
    """
    db = _FakeDB()
    ran: list[int] = []
    probes: list[int] = []

    async def _cycle(*_a, **_k):
        ran.append(1)
        return dict(cycle_stats[min(len(ran) - 1, len(cycle_stats) - 1)])

    async def _probe(_client, _reason=None):
        probes.append(1)
        return alive(len(probes))

    monkeypatch.setattr(recorder, "RecorderDB", lambda *a, **k: db)
    monkeypatch.setattr(recorder, "_load_access_token", lambda: "TOK_A")
    monkeypatch.setattr(
        recorder, "LeaderboardCache", lambda client, **k: _FakeCache(client)
    )
    monkeypatch.setattr(recorder, "run_cycle", _cycle)
    monkeypatch.setattr(recorder, "_upstream_alive", _probe)
    monkeypatch.setattr(recorder.config, "CYCLE_SECONDS", 0)  # ⇒ zero wait
    monkeypatch.setattr(recorder.config, "UPSTREAM_BREAKER_AFTER", 3)

    asyncio.run(recorder.main_loop(cycles=cycles))
    return len(ran), len(probes), db


def test_the_breaker_stops_collecting_after_three_dead_cycles(patched, monkeypatch):
    """The exact intent: 67 blocked cycles threw 190 requests a minute and wrote zero.

    After the third, the cycle becomes a single probe — knocking on a wall
    does not open it, and Cloudflare's blocking rules are extended by
    continuous knocking.
    """
    ran, probes, db = _loop_with(
        monkeypatch, [_LIVE_BLOCKED], lambda _n: False, cycles=9
    )

    assert ran == 3                  # three, then no collecting cycle after
    assert probes == 6               # and six cycles became six calls
    assert "last_error_upstream_blocked" in db.meta   # and the block is written down, not silenced


def test_the_first_answering_probe_resumes_full_cycles(patched, monkeypatch):
    """And the first success brings collection back whole: the breaker slows the knocking, not the collection."""
    ran, probes, _db = _loop_with(
        monkeypatch, [_LIVE_BLOCKED, _LIVE_BLOCKED, _LIVE_BLOCKED, _LIVE_OK],
        lambda n: n >= 3, cycles=8
    )

    assert probes == 3               # two probes refused, then a third accepted
    assert ran == 6                  # 3 dead + 3 after the return, nothing lost


def test_a_two_cycle_outage_never_opens_the_breaker(patched, monkeypatch):
    """Seven days of history: ten outages, all ≤ two cycles. None may slow anything down."""
    ran, probes, _db = _loop_with(
        monkeypatch, [_LIVE_BLOCKED, _LIVE_BLOCKED, _LIVE_OK],
        lambda _n: False, cycles=6
    )

    assert probes == 0               # the breaker never opened at all
    assert ran == 6                  # and no collecting cycle was lost
