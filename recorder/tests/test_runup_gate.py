"""Tests for the late-signal filter (MAX_PRE_SIGNAL_RUNUP)."""
import pytest
from db import RecorderDB

import recorder


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), "schema.sql")
    yield value
    value.close()


def _bars(db: RecorderDB, token: str, *, start_px: float, end_px: float,
          n: int = 24, end_ts: int = 1_787_600_000) -> None:
    """n completed bars before end_ts, a linear price from start_px to end_px."""
    rows = []
    for i in range(n):
        px = start_px + (end_px - start_px) * i / max(n - 1, 1)
        ts = end_ts - (n - i) * 300
        rows.append({
            "token_address": token, "network_id": "1399811149",
            "resolution": "5", "ts": ts, "o": px, "h": px, "l": px, "c": px,
            "v": 1.0, "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
            "fetched_at": "2026-08-27T00:00:00+00:00",
        })
    db.insert_bars(rows)


def test_late_signal_rejected(db, monkeypatch):
    """A 200% runup before the signal ⇒ watch rejected (a late liquidity trap)."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    _bars(db, "LATEx", start_px=1.0, end_px=3.0)   # log(3.0/1.0)=1.10 > 1.5? no
    _bars(db, "LATE2x", start_px=1.0, end_px=6.0)  # log(6)=1.79 > 1.5 ⇒ reject
    t0 = 1_787_600_000
    runup = recorder.pre_signal_runup(db, "LATE2x", "1399811149", t0)
    assert runup is not None and runup > 1.5
    verdict = recorder.runup_verdict(db, "LATE2x", "1399811149", t0)
    assert verdict == recorder.RUNUP_LATE


def test_early_signal_allowed(db, monkeypatch):
    """No prior runup ⇒ the signal is early and accepted."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    _bars(db, "EARLYx", start_px=1.0, end_px=1.1)
    t0 = 1_787_600_000
    verdict = recorder.runup_verdict(db, "EARLYx", "1399811149", t0)
    assert verdict == recorder.RUNUP_OK


def test_no_history_is_allowed(db, monkeypatch):
    """No bars before the signal (a brand-new coin) ⇒ runup unknown, accepted:
    the gate rules on documented lateness, not on ignorance — and a recently
    pumping coin with no history is precisely the early signal we want."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    t0 = 1_787_600_000
    verdict = recorder.runup_verdict(db, "NOBARx", "1399811149", t0)
    assert verdict == recorder.RUNUP_OK


def test_filter_disabled_when_zero(db, monkeypatch):
    """MAX_PRE_SIGNAL_RUNUP=0 disables the filter entirely (pre-2026-08-27 behavior)."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 0)
    _bars(db, "ANYLATE", start_px=1.0, end_px=10.0)
    t0 = 1_787_600_000
    verdict = recorder.runup_verdict(db, "ANYLATE", "1399811149", t0)
    assert verdict == recorder.RUNUP_OK


async def test_record_feed_rejects_late(db, monkeypatch, policy):
    """Integration: a signal on a coin that ran up 3x before it opens no watch,
    and is counted in the counter."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    monkeypatch.setattr(recorder.config, "MIN_TOKEN_AGE_DAYS", 0)
    _bars(db, "VERYLATE", start_px=1.0, end_px=8.0)   # log(8)=2.08
    import datetime

    now_iso = datetime.datetime.fromtimestamp(
        1_787_600_000, datetime.UTC).isoformat()
    stats = {k: 0 for k in (
        "signals", "watch_added", "age_rejected", "age_unknown",
        "runup_rejected", "errors", "evm_admission_deferred")}
    raw = {"responseObject": {"data": [{
        "id": "e-late", "tokenAddress": "VERYLATE", "networkId": 1399811149,
        "type": "large_buy", "createdAt": "1787600000",
        "body": {"price": 8.0},
    }]}}
    admitted = await recorder.record_feed(
        db, raw, now_iso, lambda _id: None, {}, policy, stats,
    )
    assert stats["signals"] == 1            # the signal is always kept
    assert stats["watch_added"] == 0        # but the watch is rejected
    assert stats["runup_rejected"] == 1
    assert admitted == set()


@pytest.fixture()
def policy():
    return recorder.EVMAdmissionPolicy(frozenset(), 0, 1, 1, False)
