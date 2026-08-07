import os

import pytest

import backfill_bars as bb
import backfill_training_ath as bta
import features
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield value
    value.close()


def _token(db, token="tok", network="56"):
    db._conn.execute(
        """INSERT INTO signal_events(
               id,token_address,network_id,ts,recorded_at,signal_type,raw_json)
           VALUES(?,?,?,?,?,?,?)""",
        ("e", token, network, "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:01Z", "large_buy", "{}"),
    )
    db._conn.commit()


def _env(ts):
    n = len(ts)
    return {"responseObject": {
        "s": "ok", "t": ts, "o": [1.0] * n, "h": [2.0] * n,
        "l": [0.5] * n, "c": [1.5] * n, "v": [9.0] * n,
    }}


async def _noop(_seconds):
    pass


async def test_backfill_pages_backwards_then_marks_complete(db, monkeypatch):
    _token(db)
    calls = []

    async def fake_fetch(client, token, network, start, end, resolution):
        calls.append((start, end, resolution))
        if len(calls) == 1:
            return _env([1_700_000_000, 1_700_086_400])
        return {"responseObject": {"s": "no_data", "t": []}}

    monkeypatch.setattr(bb, "_fetch_bars_raw", fake_fetch)
    stats = await bb.run(None, db, max_calls=5, sleep=_noop)
    state = db._conn.execute(
        "SELECT * FROM historical_bars_state WHERE token_address='tok'"
    ).fetchone()
    assert stats["done"] == 1
    assert stats["calls"] == 2
    assert state["last_status"] == "ok"
    assert state["cursor_to"] == 1_699_999_999
    assert calls[0][2] == "1D"
    assert db._conn.execute(
        "SELECT COUNT(*) FROM token_bars WHERE resolution='1D'"
    ).fetchone()[0] == 2


async def test_empty_history_needs_three_attempts_before_terminal(db, monkeypatch):
    _token(db)

    async def no_data(*_args):
        return {"responseObject": {"s": "no_data", "t": []}}

    monkeypatch.setattr(bb, "_fetch_bars_raw", no_data)
    for _ in range(bb.MAX_EMPTY_ATTEMPTS):
        await bb.run(None, db, max_calls=1, sleep=_noop)
    state = db._conn.execute(
        "SELECT * FROM historical_bars_state WHERE token_address='tok'"
    ).fetchone()
    assert state["last_status"] == "no_data"
    assert state["attempts"] == bb.MAX_EMPTY_ATTEMPTS


async def test_dry_run_does_not_write(db):
    _token(db)
    stats = await bb.run(None, db, max_calls=10, dry_run=True)
    assert stats["tokens"] == 1 and stats["partial"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM historical_bars_state").fetchone()[0] == 0


async def test_expired_session_stops_without_poisoning_remaining_tokens(db, monkeypatch):
    _token(db, token="a")
    db._conn.execute(
        """INSERT INTO signal_events(
               id,token_address,network_id,ts,recorded_at,signal_type,raw_json)
           VALUES(?,?,?,?,?,?,?)""",
        ("e2", "b", "56", "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:01Z", "large_buy", "{}"),
    )
    db._conn.commit()

    class UnauthorizedError(Exception):
        pass

    async def expired(*_args):
        raise UnauthorizedError("expired")

    monkeypatch.setattr(bb, "_fetch_bars_raw", expired)
    stats = await bb.run(None, db, max_calls=10, sleep=_noop)
    assert stats["stopped_reason"] == "session_expired"
    assert stats["calls"] == 1
    assert db._conn.execute(
        "SELECT COUNT(*) FROM historical_bars_state"
    ).fetchone()[0] == 1


async def test_transient_upstream_failure_retries_same_token(db, monkeypatch):
    _token(db)
    seen = 0

    class UpstreamUnavailableError(Exception):
        pass

    async def flaky(*_args):
        nonlocal seen
        seen += 1
        if seen == 1:
            raise UpstreamUnavailableError("temporary")
        return {"responseObject": {"s": "no_data", "t": []}}

    monkeypatch.setattr(bb, "_fetch_bars_raw", flaky)
    stats = await bb.run(None, db, max_calls=3, sleep=_noop)
    assert stats["calls"] == 2 and stats["retries"] == 1
    assert stats["errors"] == 0
    assert db._conn.execute(
        "SELECT last_status FROM historical_bars_state"
    ).fetchone()[0] == "empty_retry"


def test_daily_threshold_keeps_realistic_pump_and_rejects_impossible_spike(db):
    rows = []
    for i, high in enumerate((1.1, 100.0, 1.1, 2_000_000.0, 1.1)):
        rows.append({
            "token_address": "tok", "network_id": "56", "resolution": "1D",
            "ts": 1_700_000_000 + i * 86400, "o": 1.0, "h": high,
            "l": 0.9, "c": 1.0, "v": 1.0, "h_suspect": 0,
            "l_suspect": 0, "c_suspect": 0, "fetched_at": "t",
        })
    db.insert_bars(rows)
    db.recompute_bar_flags("tok", "56", "1D")
    flags = db._conn.execute(
        "SELECT h_suspect FROM token_bars WHERE resolution='1D' ORDER BY ts"
    ).fetchall()
    assert flags[1][0] == 0
    assert flags[3][0] == 1


def test_training_ath_upgrade_matches_complete_daily_history(db):
    t0 = 1_700_500_000
    db.insert_bars([
        {
            "token_address": "tok", "network_id": "56", "resolution": "5",
            "ts": t0 - 600, "o": 1.0, "h": 2.0, "l": 0.8, "c": 1.0,
            "v": 1.0, "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
            "fetched_at": "t",
        },
        {
            "token_address": "tok", "network_id": "56", "resolution": "1D",
            "ts": t0 - 3 * 86400, "o": 5.0, "h": 10.0, "l": 4.0, "c": 5.0,
            "v": 1.0, "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
            "fetched_at": "t",
        },
    ])
    db._conn.execute(
        """INSERT INTO historical_bars_state(
               token_address,network_id,resolution,cursor_to,oldest_ts,last_status,
               candles,calls,attempts,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("tok", "56", "1D", 0, t0 - 3 * 86400, "ok", 1, 1, 1, "t"),
    )
    db._conn.execute(
        """INSERT INTO training_rows(
               kind,key,token_address,network_id,entry_ts,dist_from_ath,
               feature_version,built_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        ("signal", "e", "tok", "56", t0, -0.5, 2, "t"),
    )
    db._conn.commit()
    assert bta.upgrade(db, force=True)["updated"] == 1
    row = db._conn.execute("SELECT * FROM training_rows WHERE key='e'").fetchone()
    assert row["feature_version"] == features.FEATURE_VERSION
    assert row["ath_history_complete"] == 1
    assert row["dist_from_ath"] == pytest.approx(-0.9)
