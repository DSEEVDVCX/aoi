"""Tests for the two new labels: is_explosive and time_to_plus20_min (fv15)."""

import labeler


def _bars(closes: list[float], entry_ts: int = 1_787_600_000, step: int = 300):
    """Simple bars: closes after the entry point, spaced step seconds apart."""
    bars = []
    for i, c in enumerate(closes):
        ts = entry_ts + (i + 1) * step
        bars.append({"ts": ts, "o": c, "h": c, "l": c, "c": c, "v": 1.0,
                     "h_suspect": 0, "l_suspect": 0, "c_suspect": 0})
    return bars


def test_explosive_label_true_when_peak_and_speed_met():
    """Peak >= +100% with half of it (+50%) within the first 24h => is_explosive=1."""
    # 30 bars (2.5 hours): climbs to +50% in the first 12 bars (1 hour),
    # then peaks at +120%
    closes = [1.0] * 2 + [1.5] * 10 + [2.2] * 18
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["is_explosive"] == 1


def test_explosive_false_when_peak_only_80pct():
    """Peak of +80% (< 2x) => not explosive even if fast."""
    closes = [1.0] * 2 + [1.8] * 28
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["is_explosive"] == 0


def test_explosive_false_when_slow_explosion():
    """Peak of +150% but +50% not reached within 24h (all in day two) => 0.

    A slow signal is marginal hunting (measured 2026-08-28: their availability
    within the first day is +26% on average) — the label separates "a
    catchable explosion" from "a late peak".
    """
    # 48h = 576 bars. The first 300 bars (25h) sit quiet at 1.0-1.1, the last
    # of them jumps to 2.5
    closes = [1.0] * 300 + [1.1] * 200 + [2.5] * 76
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    # The 2.5x peak happened, but max_gain_24h = 1.1/1.0-1 = 10% < 50%
    assert out["max_gain_48h"] > 1.0
    assert out["is_explosive"] == 0


def test_time_to_plus20_measured_in_minutes():
    """time_to_plus20_min: minutes until the first close >= +20% (a drop filter, not an entry)."""
    # Bar 4 (20 minutes) reaches 1.21
    closes = [1.0, 1.05, 1.1, 1.21, 1.3, 1.5]
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["time_to_plus20_min"] == 20


def test_time_to_plus20_null_when_never_reached():
    """Never reached +20% at all => NULL (absence, not infinity)."""
    closes = [1.0, 1.05, 1.1, 1.15, 1.18]
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["time_to_plus20_min"] is None


def test_explosive_null_when_gains_missing():
    """A window without enough bars to judge => both labels NULL (no fabricated verdict)."""
    out = labeler.compute_labels(bars=[], entry_ts=1_787_600_000)
    assert out["status"] != "ok"
    assert out["is_explosive"] is None
    assert out["time_to_plus20_min"] is None


def test_labels_not_in_feature_columns():
    """Leakage guard: the two labels are in LABEL_COLUMNS, never FEATURE_COLUMNS."""
    import features

    assert "is_explosive" in features.LABEL_COLUMNS
    assert "time_to_plus20_min" in features.LABEL_COLUMNS
    assert "is_explosive" not in features.FEATURE_COLUMNS
    assert "time_to_plus20_min" not in features.FEATURE_COLUMNS
