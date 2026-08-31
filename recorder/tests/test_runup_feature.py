"""Tests for the pre_signal_runup feature (fv13) — runup position as a continuous measurement."""
import features
import pytest
from db import RecorderDB


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), "schema.sql")
    yield value
    value.close()


def _bars(db: RecorderDB, token: str, closes: list[float], *,
          end_ts: int = 1_787_600_000) -> None:
    """Completed bars before end_ts, with `closes` as their closes in chronological order."""
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


def test_runup_matches_window_start(db):
    """Runup from the first bar in a 24h window to the last close before t0."""
    # 24 bars from 1.0 to 2.0: runup = log(2.0/1.0)
    closes = [1.0 + i / 23 for i in range(24)]
    _bars(db, "RUNx", closes)
    out = features.price_history_features(db, "RUNx", "1399811149", T0)
    import math

    assert out["pre_signal_runup"] == pytest.approx(math.log(2.0), rel=1e-6)


def test_runup_uses_only_last_24h(db):
    """Bars older than 24 hours do not enter the numerator — the window is 288 bars, not all of history."""
    # 300 bars (25 hours): the very old ones must be excluded from the 24h window
    closes = [10.0] * 12 + [1.0 + i / 275 for i in range(276)]
    _bars(db, "WINx", closes)
    out = features.price_history_features(db, "WINx", "1399811149", T0)
    import math

    # The window = the last 288 bars: starts from ~1.5, not 10.0
    window_first = closes[-288]
    expected = math.log(closes[-1] / window_first)
    assert out["pre_signal_runup"] == pytest.approx(expected, rel=1e-6)


def test_runup_null_when_insufficient_history(db):
    """Fewer than 12 bars ⇒ NULL (a measured absence, not zero)."""
    _bars(db, "NEWx", [1.0, 1.1, 1.2])
    out = features.price_history_features(db, "NEWx", "1399811149", T0)
    assert out["pre_signal_runup"] is None


def test_runup_zero_for_flat_history(db):
    """A full history with no movement ⇒ exactly 0.0 (a measurement, not an absence)."""
    _bars(db, "FLATx", [1.0] * 24)
    out = features.price_history_features(db, "FLATx", "1399811149", T0)
    assert out["pre_signal_runup"] == pytest.approx(0.0, abs=1e-9)


def test_feature_declared_in_columns():
    """The column is a first-class citizen in FEATURE_COLUMNS and fv13."""
    assert "pre_signal_runup" in features.FEATURE_COLUMNS
    assert features.FEATURE_VERSION >= 13


def test_runup_never_uses_post_t0_bars(db):
    """The point-in-time law: bars after t0 never enter the calculation."""
    # 24 quiet bars before t0, then 12 rocket bars after it
    before = [1.0 + i / 23 * 0.1 for i in range(24)]
    _bars(db, "LEAKx", before, end_ts=T0)
    rows = [{
        "token_address": "LEAKx", "network_id": "1399811149",
        "resolution": "5", "ts": T0 + (i + 1) * 300,
        "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 1.0,
        "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
        "fetched_at": "2026-08-27T00:00:00+00:00",
    } for i in range(12)]
    db.insert_bars(rows)
    out = features.price_history_features(db, "LEAKx", "1399811149", T0)
    import math

    expected = math.log(before[-1] / before[0])
    assert out["pre_signal_runup"] == pytest.approx(expected, rel=1e-6)
