"""Tests for the runup gate counter + re-evaluation backoff."""
import pytest
from db import RecorderDB

import recorder


@pytest.fixture()
def db(tmp_path):
    recorder._RUNUP_STATE = {}          # reset the in-memory state between tests
    value = RecorderDB(str(tmp_path / "t.db"), "schema.sql")
    yield value
    value.close()
    recorder._RUNUP_STATE = {}


def _bars(db: RecorderDB, token: str, closes: list[float], *,
          end_ts: int = 1_787_600_000) -> None:
    n = len(closes)
    rows = []
    for i, px in enumerate(closes):
        rows.append({
            "token_address": token, "network_id": "1399811149",
            "resolution": "5", "ts": end_ts - (n - i) * 300,
            "o": px, "h": px, "l": px, "c": px, "v": 1.0,
            "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
            "fetched_at": "2026-08-27T00:00:00+00:00",
        })
    db.insert_bars(rows)


T0 = 1_787_600_000


def test_rejected_runup_is_counted_total(db, monkeypatch):
    """Rejections are persisted cumulatively in meta — a silent gate is blind (found 2026-08-28)."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    _bars(db, "LATE1x", [1.0 + i * 0.3 for i in range(24)])   # log(8)=2.08
    stats = {"runup_rejected": 3}
    recorder._persist_runup_rejection(db, stats)
    assert db.get_meta("runup_rejected_total") == "3"
    stats2 = {"runup_rejected": 2}
    recorder._persist_runup_rejection(db, stats2)
    assert db.get_meta("runup_rejected_total") == "5"          # cumulative, not replaced


def test_zero_rejection_writes_nothing(db):
    """A cycle with no rejection does not touch meta — no write noise."""
    recorder._persist_runup_rejection(db, {"runup_rejected": 0})
    assert db.get_meta("runup_rejected_total") is None


def test_runup_backoff_caches_late_verdict(db, monkeypatch):
    """A "late" verdict is stored and not recomputed on every signal — backoff like the age gate.

    A running-up coin receives 5-10 signals a day and each one used to re-query
    the bars. Now: the verdict is stored for `RUNUP_RETRY_SECONDS` and only
    re-evaluated after it (the runup may calm down, making a later signal
    genuinely early).
    """
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    monkeypatch.setattr(recorder.config, "RUNUP_RETRY_SECONDS", 3600)
    _bars(db, "HOT1x", [1.0 + i * 0.3 for i in range(24)])
    # First verdict: computed and stored
    v1 = recorder.runup_verdict(db, "HOT1x", "1399811149", T0)
    assert v1 == recorder.RUNUP_LATE
    assert ("HOT1x", "1399811149") in recorder._RUNUP_STATE
    # A signal a minute later: served from memory, no recomputation (same t0 ⇒
    # same result, but the query did not run — verified by changing the bars
    # under its feet)
    db._conn.execute(
        "DELETE FROM token_bars WHERE token_address='HOT1x'")
    db._commit()
    v2 = recorder.runup_verdict(db, "HOT1x", "1399811149", T0 + 60)
    assert v2 == recorder.RUNUP_LATE            # from the cache despite the bars being deleted


def test_runup_backoff_expires(db, monkeypatch):
    """After RUNUP_RETRY_SECONDS the re-evaluation actually happens (the runup may calm down)."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    monkeypatch.setattr(recorder.config, "RUNUP_RETRY_SECONDS", 3600)
    _bars(db, "COOLx", [1.0 + i * 0.3 for i in range(24)])
    assert recorder.runup_verdict(db, "COOLx", "1399811149", T0) == recorder.RUNUP_LATE
    # After an hour+: the bars are deleted (no history) ⇒ early by definition = a re-evaluation happened
    db._conn.execute("DELETE FROM token_bars WHERE token_address='COOLx'")
    db._commit()
    v = recorder.runup_verdict(db, "COOLx", "1399811149", T0 + 3700)
    assert v == recorder.RUNUP_OK


def test_ok_verdict_not_cached(db, monkeypatch):
    """An "ok" verdict is not stored: runup changes fast and the next candidate deserves a fresh measurement."""
    monkeypatch.setattr(recorder.config, "MAX_PRE_SIGNAL_RUNUP", 1.5)
    monkeypatch.setattr(recorder.config, "RUNUP_RETRY_SECONDS", 3600)
    _bars(db, "OKAYx", [1.0] * 24)
    assert recorder.runup_verdict(db, "OKAYx", "1399811149", T0) == recorder.RUNUP_OK
    assert ("OKAYx", "1399811149") not in recorder._RUNUP_STATE
