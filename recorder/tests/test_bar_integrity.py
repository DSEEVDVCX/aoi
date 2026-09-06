"""Bar integrity tests: distortion coming from upstream.

The two reference incidents (2026-07-30, both confirmed by a live re-fetch):
1. An impossible `h`: 2,626,092.02 on a bar whose close was 0.021948 (×119
   million) ⇒ the dashboard displayed +62,570,743,609%.
2. The close itself distorted: 12,052.5 between two closes of ≈0.0004 ⇒
   `max_gain` = +2.1 billion%.

The principle under test: **price is continuous** — a jump that does not
continue into the neighbor is distortion, not price.
"""
import extract
from labeler import compute_labels

FETCHED = "2026-07-30T10:35:02+00:00"


def _env(bars):
    """(ts, o, h, l, c) → a getBarsNew envelope with parallel arrays."""
    return {"responseObject": {
        "s": "ok",
        "t": [b[0] for b in bars], "o": [b[1] for b in bars],
        "h": [b[2] for b in bars], "l": [b[3] for b in bars],
        "c": [b[4] for b in bars], "v": [1.0] * len(bars),
    }}


def _series(*rows):
    """(o, h, l, c) each → an ordered series with 5-minute timestamps."""
    return [
        {"ts": 1785394200 + i * 300, "o": o, "h": h, "l": low, "c": c}
        for i, (o, h, low, c) in enumerate(rows)
    ]


# --- the isolated check (a bar without neighbors) ---
def test_isolated_real_candle_is_not_flagged():
    assert extract.bar_wick_flags(0.0040, 0.0045, 0.0039, 0.0040) == (0, 0)


def test_isolated_big_but_plausible_wick_is_kept():
    """A real ×5 wick — the ×10 threshold is measured on 651k bars."""
    assert extract.bar_wick_flags(0.001, 0.005, 0.0009, 0.001) == (0, 0)


def test_isolated_impossible_high_is_flagged():
    assert extract.bar_wick_flags(0.00403, 2626092.02, 0.00394, 0.02194)[0] == 1


def test_isolated_impossible_low_is_flagged():
    assert extract.bar_wick_flags(0.01, 0.011, 1e-9, 0.01)[1] == 1


def test_isolated_missing_or_zero_never_flags_nor_divides_by_zero():
    assert extract.bar_wick_flags(None, None, None, None) == (0, 0)
    assert extract.bar_wick_flags(0.0, 1.0, 0.0, 0.0) == (0, 0)


# --- the neighbor verdict: the two real cases ---
def test_context_flags_impossible_high_case():
    """h = 2,626,092 with a clean close of 0.0219 and neighbors 0.0040/0.0202."""
    flags = extract.bar_context_flags(_series(
        (0.00402, 0.00403, 0.00401, 0.00403),
        (0.00403, 2626092.02, 0.00394, 0.02194),
        (0.02194, 0.02261, 0.01721, 0.02026),
    ))
    assert flags[1] == (1, 0, 0)          # only the high is distorted
    assert flags[0] == (0, 0, 0) and flags[2] == (0, 0, 0)


def test_context_flags_corrupt_close_case():
    """The close itself 12,052.5 between 0.000358 and 0.000395 — it does not
    continue ⇒ distorted."""
    flags = extract.bar_context_flags(_series(
        (0.000501, 0.000501, 0.000358, 0.000358),
        (0.000358, 12052.5, 0.000301, 12052.5),
        (12052.5, 12052.5, 0.000394, 0.000394),
    ))
    assert flags[1] == (1, 0, 1)          # both the close and the high
    # The next bar's close is clean, but its high inherited the corrupt value ⇒ flagged
    assert flags[2][0] == 1 and flags[2][2] == 0


def test_context_keeps_real_persistent_pump():
    """A ×5.4 jump that **persisted** (the real MarsCoin) — price, not distortion."""
    flags = extract.bar_context_flags(_series(
        (0.00402, 0.00403, 0.00401, 0.00403),
        (0.00403, 0.02200, 0.00394, 0.02194),
        (0.02194, 0.02261, 0.01721, 0.02026),
    ))
    assert all(f == (0, 0, 0) for f in flags)


def test_context_keeps_real_rug_dump():
    """A 99% dump that **persisted** = a real rug that must stay for correct labels."""
    flags = extract.bar_context_flags(_series(
        (0.010, 0.011, 0.0099, 0.010),
        (0.010, 0.010, 0.00009, 0.0001),
        (0.0001, 0.00011, 0.00009, 0.0001),
    ))
    assert all(f == (0, 0, 0) for f in flags)


def test_context_flags_non_persistent_dip_in_close():
    flags = extract.bar_context_flags(_series(
        (0.010, 0.011, 0.0099, 0.010),
        (0.010, 0.010, 1e-9, 1e-9),        # a microscopic close that immediately recovers
        (1e-9, 0.011, 0.0099, 0.010),
    ))
    assert flags[1][2] == 1


def test_context_single_bar_falls_back_to_isolated_check():
    flags = extract.bar_context_flags(_series((0.004, 2626092.02, 0.0039, 0.0219)))
    assert flags[0][0] == 1


# --- extraction carries the flags and keeps the raw values ---
def test_extract_bars_flags_and_keeps_raw():
    rows = extract.extract_bars(
        _env([
            (1785393900, 0.004015, 0.0040339, 0.004015, 0.0040308),
            (1785394200, 0.0040308, 2626092.022316815, 0.0039456, 0.021948),
            (1785394500, 0.021948, 0.022612, 0.017212, 0.020263),
        ]),
        "0xtok", "56", "5", FETCHED,
    )
    assert rows[1]["h_suspect"] == 1
    assert rows[1]["h"] == 2626092.022316815   # the raw value as received — never fixed, never deleted
    assert rows[1]["c"] == 0.021948
    assert rows[0]["h_suspect"] == 0 and rows[2]["h_suspect"] == 0


# --- the labeler is not poisoned ---
ENTRY = 1785356400


def _bar(ts, h, c, low=None, hs=0, ls=0, cs=0):
    return {"ts": ts, "o": c, "h": h, "l": low if low is not None else c, "c": c,
            "h_suspect": hs, "l_suspect": ls, "c_suspect": cs}


def test_labels_ignore_suspect_high():
    bars = [
        _bar(ENTRY, 0.0042, 0.0042),
        _bar(ENTRY + 300, 0.0050, 0.0048),
        _bar(ENTRY + 600, 2626092.0, 0.0219, hs=1),
        _bar(ENTRY + 900, 0.0230, 0.0225),
    ]
    out = compute_labels(bars, ENTRY)
    assert out["status"] == "ok"
    assert out["max_gain_48h"] == 0.0230 / 0.0042 - 1     # no billions
    assert out["suspect_bars"] == 1
    assert out["time_to_peak_h"] == 900 / 3600


def test_labels_ignore_suspect_close_in_final_return():
    bars = [
        _bar(ENTRY, 0.010, 0.010),
        _bar(ENTRY + 300, 0.011, 0.011),
        _bar(ENTRY + 600, 12052.5, 12052.5, hs=1, cs=1),   # last bar distorted entirely
    ]
    out = compute_labels(bars, ENTRY)
    assert out["final_return_48h"] == 0.011 / 0.010 - 1    # from the last clean close
    assert out["is_rug"] == 0


def test_labels_skip_suspect_entry_close():
    """The entry close is distorted ⇒ skip to the first clean one instead of
    a corrupt denominator."""
    bars = [
        _bar(ENTRY, 12052.5, 12052.5, hs=1, cs=1),
        _bar(ENTRY + 300, 0.011, 0.010),
        _bar(ENTRY + 600, 0.012, 0.011),
    ]
    out = compute_labels(bars, ENTRY)
    assert out["entry_px"] == 0.010
    assert out["final_return_48h"] == 0.011 / 0.010 - 1


def test_labels_ignore_suspect_low_in_drawdown():
    bars = [
        _bar(ENTRY, 0.01, 0.01),
        _bar(ENTRY + 300, 0.011, 0.01, low=1e-9, ls=1),
        _bar(ENTRY + 600, 0.011, 0.009, low=0.008),
    ]
    out = compute_labels(bars, ENTRY)
    assert out["max_drawdown_48h"] == 0.008 / 0.01 - 1     # not -100%
    assert out["suspect_bars"] == 1


def test_all_highs_suspect_leaves_gain_none_not_fabricated():
    bars = [_bar(ENTRY, 0.01, 0.01), _bar(ENTRY + 300, 9e9, 0.01, hs=1)]
    out = compute_labels(bars, ENTRY)
    assert out["max_gain_48h"] is None
    assert out["time_to_peak_h"] is None
    assert out["final_return_48h"] == 0.0


def test_clean_bars_unchanged_by_the_fix():
    """Bars without flags: exactly the previous results (no regression)."""
    bars = [
        {"ts": ENTRY, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0},
        {"ts": ENTRY + 300, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5},
    ]
    out = compute_labels(bars, ENTRY)
    assert out["max_gain_48h"] == 1.0
    assert out["max_drawdown_48h"] == -0.5
    assert out["final_return_48h"] == 0.5
    assert out["suspect_bars"] == 0
