"""Tests for the retro bar fetch and labeling of activity events (no network).

Locks in: time-cluster merging and splitting, the status='ok' gate before
labeling (no eternal no_entry before bars arrive), writing outcomes with
kind='activity' and the independence flag, and the ok/no_data/error paths of
a fetch round.
"""
import os

import backfill_activity_bars as bab
import labeler
import pytest
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
T0 = 1_761_200_000  # a nominal event time


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


# --- clusters ---
def test_clusters_empty_and_single():
    assert bab.event_clusters([], 48, 900) == []
    one = bab.event_clusters([T0], 48, 900)
    assert one == [(T0 - 3600, T0 + 48 * 3600 + 900)]


def test_clusters_merge_overlapping_and_split_on_gap():
    w = 48 * 3600
    merged = bab.event_clusters([T0, T0 + 10 * 3600], 48, 900)   # 10h < 48h
    assert merged == [(T0 - 3600, T0 + 10 * 3600 + w + 900)]
    split = bab.event_clusters([T0, T0 + w + 3600], 48, 900)     # gap > 48h
    assert len(split) == 2
    # The second cluster covers the second event's window fully (the margin
    # overlap is deliberate — INSERT OR REPLACE removes duplicate bars)
    assert split[1][0] <= T0 + w + 3600
    assert split[1][1] >= T0 + w + 3600 + w


def test_clusters_chain_merging():
    # 0, 20h, 40h: each within the previous window ⇒ one extended cluster
    ts = [T0, T0 + 20 * 3600, T0 + 40 * 3600]
    assert bab.event_clusters(ts, 48, 900) == [(T0 - 3600, T0 + 40 * 3600 + 48 * 3600 + 900)]


def test_chunks_split_long_windows():
    chunks = bab._chunks(0, 150 * 3600, bab._CHUNK_SECONDS)
    assert chunks == [(0, 72 * 3600), (72 * 3600, 144 * 3600), (144 * 3600, 150 * 3600)]


# --- seeding helpers ---
def _event(db, eid, tok="tokA", net="56", ts="2025-11-01T00:00:00+00:00"):
    db.insert_activity_events([{
        "id": eid, "event_type": "multi_user_buy", "token_address": tok,
        "network_id": net, "ts": ts, "recorded_at": ts, "raw_json": "{}",
    }])


def _bars(db, tok, net, start, n, step=300):
    rows = [{
        "token_address": tok, "network_id": net, "resolution": "5",
        "ts": start + i * step, "o": 1.0, "h": 2.0 + i * 0.1, "l": 0.5,
        "c": 1.0 + i * 0.1, "v": 9.0, "fetched_at": "t",
    } for i in range(n)]
    db.insert_bars(rows)


# --- the labeling gate ---
def test_pending_label_requires_bars_state_ok(db):
    _event(db, "e1")
    mature = 10**12
    assert db.activities_pending_label(mature, 10) == []        # no state yet
    db.set_activity_bars_state("tokA", "56", "no_data", 0, "t")
    assert db.activities_pending_label(mature, 10) == []        # no_data is not enough
    db.set_activity_bars_state("tokA", "56", "ok", 500, "t")
    rows = db.activities_pending_label(mature, 10)
    assert [r["id"] for r in rows] == ["e1"]                    # ok ⇒ ready


def test_pending_label_respects_maturity_and_existing_outcome(db):
    _event(db, "e1", ts="2025-11-01T00:00:00+00:00")
    db.set_activity_bars_state("tokA", "56", "ok", 1, "t")
    assert db.activities_pending_label(1, 10) == []             # not mature
    db.insert_outcome({
        "kind": "activity", "key": "e1", "token_address": "tokA", "network_id": "56",
        "signal_type": "multi_user_buy", "is_control": 0, "is_independent": 1,
        "entry_ts": 1, "split": "train", "status": "no_bars", "labeled_at": "t",
    })
    assert db.activities_pending_label(10**12, 10) == []        # labeled, not re-labeled


def test_label_pending_writes_activity_outcome_with_independence(db):
    _event(db, "e1", ts="2025-11-01T00:00:00+00:00")
    _event(db, "e2", ts="2025-11-01T00:10:00+00:00")            # 10 minutes later
    db.set_activity_bars_state("tokA", "56", "ok", 1, "t")
    entry = labeler._epoch("2025-11-01T00:00:00+00:00")
    _bars(db, "tokA", "56", entry, 600)                          # covers 50 hours

    stats = labeler.label_pending(db, now_epoch=entry + 50 * 3600)
    assert stats["activities"] == 2
    rows = db._conn.execute(
        "SELECT key, kind, is_independent, status FROM outcomes WHERE kind='activity' ORDER BY key"
    ).fetchall()
    assert [r["key"] for r in rows] == ["e1", "e2"]
    assert rows[0]["is_independent"] == 1                        # first event
    assert rows[1]["is_independent"] == 0                        # 10 minutes < 30
    assert rows[0]["status"] == "ok"


# --- the fetch round (monkeypatched call) ---
def _bars_env(n=3, base=1000):
    return {"responseObject": {
        "s": "ok", "t": [base + i * 300 for i in range(n)],
        "o": [1.0] * n, "h": [2.0] * n, "l": [0.5] * n, "c": [1.5] * n, "v": [9.0] * n,
    }}


async def _noop(_s):
    pass


async def test_run_fetches_clusters_and_marks_ok(db, monkeypatch):
    _event(db, "e1", ts="2025-11-01T00:00:00+00:00")
    calls = []

    async def fake_fetch(client, addr, net, f, t, resolution=None):
        calls.append((addr, f, t))
        return _bars_env()

    monkeypatch.setattr(bab, "_fetch_bars_raw", fake_fetch)
    stats = await bab.run(None, db, max_calls=100, sleep=_noop)  # type: ignore[arg-type]

    assert stats["tokens_done"] == 1
    assert stats["calls"] == 1                                   # one event = one cluster = one call
    assert db.bars_count("tokA", "56") == 3
    assert db.activity_bars_state("tokA", "56")["last_status"] == "ok"
    # rerun the round: completed tokens are skipped with no call
    stats2 = await bab.run(None, db, max_calls=100, sleep=_noop)  # type: ignore[arg-type]
    assert stats2["skipped_ok"] == 1 and len(calls) == 1


async def test_run_no_data_marks_and_eventually_skips(db, monkeypatch):
    _event(db, "e1", ts="2025-11-01T00:00:00+00:00")

    async def fake_fetch(client, addr, net, f, t, resolution=None):
        return {"responseObject": {"s": "no_data", "t": []}}

    monkeypatch.setattr(bab, "_fetch_bars_raw", fake_fetch)
    for _ in range(bab._MAX_ATTEMPTS):
        await bab.run(None, db, max_calls=100, sleep=_noop)  # type: ignore[arg-type]
    assert db.activity_bars_state("tokA", "56")["attempts"] == bab._MAX_ATTEMPTS
    stats = await bab.run(None, db, max_calls=100, sleep=_noop)  # type: ignore[arg-type]
    assert stats["skipped_dead"] == 1                            # reached the cap ⇒ dead


async def test_run_error_marked_and_retried(db, monkeypatch):
    _event(db, "e1", ts="2025-11-01T00:00:00+00:00")

    async def boom(client, addr, net, f, t, resolution=None):
        raise RuntimeError("upstream boom")

    monkeypatch.setattr(bab, "_fetch_bars_raw", boom)
    stats = await bab.run(None, db, max_calls=100, sleep=_noop)  # type: ignore[arg-type]
    assert stats["tokens_error"] == 1
    assert db.activity_bars_state("tokA", "56")["last_status"] == "error"
    # the next round retries (error is not skipped)
    stats2 = await bab.run(None, db, max_calls=100, sleep=_noop)  # type: ignore[arg-type]
    assert stats2["tokens_error"] == 1 and stats2["skipped_ok"] == 0


async def test_run_max_calls_stops_resumably(db, monkeypatch):
    for i in range(3):
        _event(db, f"e{i}", tok=f"tok{i}", ts="2025-11-01T00:00:00+00:00")

    async def fake_fetch(client, addr, net, f, t, resolution=None):
        return _bars_env()

    monkeypatch.setattr(bab, "_fetch_bars_raw", fake_fetch)
    stats = await bab.run(None, db, max_calls=2, sleep=_noop)  # type: ignore[arg-type]
    assert stats["calls"] == 2 and "stopped_at" in stats
    stats2 = await bab.run(None, db, max_calls=100, sleep=_noop)  # type: ignore[arg-type]
    assert stats2["skipped_ok"] == 2                             # the two completed
    assert stats2["tokens_done"] == 1                            # and the third now


# --- simulator source: the retro set reads activity_events, not signal_events ---
def test_load_trades_activity_source_reads_retro_events(db):
    import exit_sim

    _event(db, "e1", tok="tokA", net="56", ts="2025-11-01T00:00:00+00:00")
    db.insert_activity_events([{                                  # a different type — excluded
        "id": "e2", "event_type": "multi_user_sell", "token_address": "tokB",
        "network_id": "56", "ts": "2025-11-01T00:00:00+00:00",
        "recorded_at": "t", "raw_json": "{}",
    }])
    entry = labeler._epoch("2025-11-01T00:00:00+00:00")
    _bars(db, "tokA", "56", entry, 600)
    _bars(db, "tokB", "56", entry, 600)

    trades = exit_sim.load_trades(db, source="activity")
    assert [t["token_address"] for t in trades] == ["tokA"]       # only the group buy
    assert exit_sim.load_trades(db, source="signal") == []        # no signal_events here
