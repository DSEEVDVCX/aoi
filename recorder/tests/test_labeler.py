"""Labeler tests (no network, no system clock).

Leak-proofness properties come first: no labeling before the window matures,
the entry price never precedes the signal, peaks come from the following bars
only, and the split is by token, not by row.
"""
import os
from datetime import UTC

import config
import labeler
import pytest
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")

ENTRY = 1_785_000_000                      # the moment of entry (epoch)
H = 3600


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _bar(ts, o=1.0, h=1.0, low=1.0, c=1.0, **flags):
    return {"ts": ts, "o": o, "h": h, "l": low, "c": c, **flags}


def _series(*, entry_px=1.0):
    """A series with known outcomes: entry 1.0, peak 3.0 at hour 2, trough 0.5
    at hour 30, final close 2.0."""
    bars = [_bar(ENTRY, c=entry_px)]                       # the entry bar
    bars.append(_bar(ENTRY + 30 * 60, h=1.5, c=1.2))       # inside the first hour
    bars.append(_bar(ENTRY + 2 * H, h=3.0, c=2.5))         # the peak (hour 2)
    bars.append(_bar(ENTRY + 10 * H, h=2.0, c=1.8))
    bars.append(_bar(ENTRY + 30 * H, h=1.0, low=0.5, c=0.6))  # the trough
    bars.append(_bar(ENTRY + 48 * H, h=2.2, c=2.0))        # end of the window
    return bars


# ---------- compute_labels: a pure function ----------

def test_labels_from_a_known_series():
    out = labeler.compute_labels(_series(), ENTRY)
    assert out["status"] == "ok"
    assert out["entry_px"] == 1.0
    assert out["entry_lag_s"] == 0
    assert out["max_gain_1h"] == pytest.approx(0.5)     # peak 1.5 within an hour
    assert out["max_gain_4h"] == pytest.approx(2.0)     # peak 3.0 at hour 2
    assert out["max_gain_24h"] == pytest.approx(2.0)
    assert out["max_gain_48h"] == pytest.approx(2.0)
    assert out["max_drawdown_48h"] == pytest.approx(-0.5)
    assert out["final_return_48h"] == pytest.approx(1.0)
    assert out["time_to_peak_h"] == pytest.approx(2.0)
    assert out["candles_48h"] == 5
    assert out["bars_truncated"] == 0
    assert out["is_rug"] == 0


def test_entry_bar_never_precedes_the_signal():
    """Reverse leak: a bar before the signal is not a valid entry, and its
    peaks do not count."""
    bars = [_bar(ENTRY - 300, h=99.0, c=0.1), *_series()]
    out = labeler.compute_labels(bars, ENTRY)
    assert out["entry_px"] == 1.0                        # not the earlier 0.1
    assert out["max_gain_48h"] == pytest.approx(2.0)     # not the earlier 99


def test_entry_bars_own_high_is_not_counted_as_gain():
    """The entry bar's own high may precede our execution — it does not count
    as gain."""
    bars = [_bar(ENTRY, h=50.0, c=1.0), _bar(ENTRY + H, h=1.1, c=1.05)]
    out = labeler.compute_labels(bars, ENTRY)
    assert out["max_gain_48h"] == pytest.approx(0.1)     # not x50


def test_late_entry_bar_means_no_entry():
    bars = [_bar(ENTRY + config.LABEL_ENTRY_MAX_LAG_SECONDS + 60, c=1.0)]
    out = labeler.compute_labels(bars, ENTRY)
    assert out["status"] == "no_entry"
    assert out["entry_px"] is None
    assert labeler.compute_labels([], ENTRY)["status"] == "no_entry"


def test_entry_with_nothing_after_is_no_bars():
    out = labeler.compute_labels([_bar(ENTRY, c=1.0)], ENTRY)
    assert out["status"] == "no_bars"
    assert out["candles_48h"] == 0


def test_admission_price_is_the_symmetric_watch_entry_price():
    bars = [_bar(ENTRY + 300, c=1.4), _bar(ENTRY + H, h=2.2, c=2.0)]

    out = labeler.compute_labels(bars, ENTRY, admission_price_usd=1.0)

    assert out["entry_px"] == 1.0
    assert out["entry_lag_s"] == 0
    assert out["final_return_48h"] == pytest.approx(1.0)


def test_truncated_series_is_flagged_not_dropped():
    """A dead coin is a signal, not a gap — excluding it introduces survivorship
    bias."""
    bars = [_bar(ENTRY, c=1.0), _bar(ENTRY + 2 * H, h=1.2, low=0.05, c=0.08)]
    out = labeler.compute_labels(bars, ENTRY)
    assert out["status"] == "ok"
    assert out["bars_truncated"] == 1
    assert out["last_bar_lag_h"] == pytest.approx(46.0)
    assert out["is_rug"] == 1                            # -92% ≤ the -90% threshold


def test_series_without_a_valid_peak_is_incomplete_not_ok():
    bars = [
        _bar(ENTRY, c=1.0),
        _bar(ENTRY + H, h=1.1, c=1.0, h_suspect=1),
    ]

    out = labeler.compute_labels(bars, ENTRY)

    assert out["status"] == "incomplete"
    assert out["max_gain_24h"] is None
    assert out["final_return_48h"] == pytest.approx(0.0)
    assert out["is_rug"] == 0


def test_zero_entry_price_is_rejected_not_divided_by():
    bars = [_bar(ENTRY, c=0.0), _bar(ENTRY + H, c=1.0)]
    assert labeler.compute_labels(bars, ENTRY)["status"] == "no_entry"


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_admission_price_is_rejected(price):
    assert labeler.compute_labels(
        _series(), ENTRY, admission_price_usd=price
    )["status"] == "no_entry"


# ---------- The split ----------

def test_split_is_deterministic_and_by_token():
    a = labeler.assign_split("0xAbCd")
    assert a == labeler.assign_split("0xabcd")           # letter case does not matter
    assert all(labeler.assign_split("0xAbCd") == a for _ in range(50))
    splits = {labeler.assign_split(f"tok{i}") for i in range(300)}
    assert splits == {"train", "val", "test"}            # all three splits appear


# ---------- label_pending: incremental, mature only ----------

def _seed_signal(db, sid, token, ts_iso):
    db.insert_signal({
        "id": sid, "token_address": token, "network_id": "56", "ts": ts_iso,
        "recorded_at": ts_iso, "signal_type": "large_buy", "raw_json": "{}",
        "top_trader_ids_json": "[]",
    })


def test_signal_decision_time_is_when_the_event_was_observed(db):
    source_ts = ENTRY
    observed_ts = ENTRY + 2 * H
    db.insert_signal({
        "id": "late", "token_address": "tokA", "network_id": "56",
        "ts": _iso(source_ts), "recorded_at": _iso(observed_ts),
        "signal_type": "large_buy", "raw_json": "{}",
        "top_trader_ids_json": "[]",
    })
    _seed_bars(db, "tokA", observed_ts)

    labeler.label_pending(db, now_epoch=observed_ts + 49 * H)

    row = db._conn.execute(
        "SELECT entry_ts FROM outcomes WHERE kind='signal' AND key='late'"
    ).fetchone()
    assert row["entry_ts"] == observed_ts


def _seed_bars(db, token, entry, *, final=2.0):
    db.insert_bars([
        {"token_address": token, "network_id": "56", "resolution": "5",
         "ts": entry, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "fetched_at": "t"},
        {"token_address": token, "network_id": "56", "resolution": "5",
         "ts": entry + 2 * H, "o": 1.0, "h": 3.0, "l": 0.9, "c": 2.5, "fetched_at": "t"},
        {"token_address": token, "network_id": "56", "resolution": "5",
         "ts": entry + 48 * H, "o": 2.0, "h": 2.2, "l": 1.9, "c": final, "fetched_at": "t"},
    ])


def _iso(epoch):
    from datetime import datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat()


def test_only_mature_windows_are_labeled(db):
    _seed_signal(db, "old", "tokA", _iso(ENTRY))
    _seed_signal(db, "fresh", "tokB", _iso(ENTRY + 40 * H))   # its window is not complete yet
    _seed_bars(db, "tokA", ENTRY)

    now = ENTRY + 48 * H + config.LABEL_MARGIN_SECONDS + 60
    stats = labeler.label_pending(db, now_epoch=now)

    assert stats["signals"] == 1
    keys = [r["key"] for r in db._conn.execute("SELECT key FROM outcomes WHERE kind='signal'")]
    assert keys == ["old"]                               # the fresh one left for a later cycle


def test_labeling_is_idempotent(db):
    _seed_signal(db, "s1", "tokA", _iso(ENTRY))
    _seed_bars(db, "tokA", ENTRY)
    now = ENTRY + 49 * H
    assert labeler.label_pending(db, now_epoch=now)["signals"] == 1
    assert labeler.label_pending(db, now_epoch=now)["signals"] == 0   # no re-labeling


def test_signal_outcome_carries_labels_split_and_metadata(db):
    _seed_signal(db, "s1", "tokA", _iso(ENTRY))
    _seed_bars(db, "tokA", ENTRY)
    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute("SELECT * FROM outcomes WHERE key='s1'").fetchone()
    assert row["kind"] == "signal"
    assert row["status"] == "ok"
    assert row["max_gain_48h"] == pytest.approx(2.0)
    assert row["final_return_48h"] == pytest.approx(1.0)
    assert row["split"] == labeler.assign_split("tokA")
    assert row["is_control"] == 0
    assert row["entry_ts"] == ENTRY


def test_independence_flag_uses_the_gap_between_signals(db):
    """69% of signals sit <5 minutes apart — a duplicate burst is labeled, not
    deleted."""
    _seed_signal(db, "first", "tokA", _iso(ENTRY))
    _seed_signal(db, "burst", "tokA", _iso(ENTRY + 600))       # 10 minutes later
    _seed_signal(db, "later", "tokA", _iso(ENTRY + 2 * H))     # two hours later
    _seed_bars(db, "tokA", ENTRY)
    labeler.label_pending(db, now_epoch=ENTRY + 60 * H)

    flags = {r["key"]: r["is_independent"] for r in
             db._conn.execute("SELECT key, is_independent FROM outcomes WHERE kind='signal'")}
    assert flags == {"first": 1, "burst": 0, "later": 1}


def test_watch_entries_including_control_get_labeled(db):
    db.upsert_watch("tokA", "56", "large_buy", "s1", 48, _iso(ENTRY))
    db.admit_control("tokC", "56", 48, _iso(ENTRY))
    _seed_bars(db, "tokA", ENTRY)
    _seed_bars(db, "tokC", ENTRY, final=0.05)                  # the control collapsed
    db.set_bars_state("tokA", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))
    db.set_bars_state("tokC", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    stats = labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    assert stats["watches"] == 2
    rows = {r["token_address"]: r for r in
            db._conn.execute("SELECT * FROM outcomes WHERE kind='watch'")}
    assert rows["tokA"]["is_control"] == 0
    assert rows["tokC"]["is_control"] == 1
    assert rows["tokC"]["is_rug"] == 1                         # -95%
    assert rows["tokC"]["is_independent"] is None              # not for watch rows
    assert rows["tokC"]["design_version"] == 2


def test_window_labeled_when_bars_stopped_before_watch_end(db):
    """A gappy series is labeled `ok` with the `bars_truncated` flag — not
    parked forever.

    Live measurement 2026-08-24: 1,900 mature windows unlabeled because the
    last bars fetch preceded `watch_until` by hours, while the bars themselves
    covered the window. The "last fetch" gate was blocking what it already
    had the data for.
    """
    db.upsert_watch("tokT", "56", "large_buy", "s1", 48, _iso(ENTRY))
    # A genuinely gappy series: the entry bar, then a void past the end.
    db.insert_bars([
        {"token_address": "tokT", "network_id": "56", "resolution": "5",
         "ts": ENTRY, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "fetched_at": "t"},
        {"token_address": "tokT", "network_id": "56", "resolution": "5",
         "ts": ENTRY + 3 * H, "o": 1.0, "h": 1.4, "l": 0.9, "c": 1.2,
         "fetched_at": "t"},
    ])
    # The last fetch is 2h before the window's end — the old gate blocks this case.
    db.set_bars_state("tokT", "56", "ok", 3, _iso(ENTRY + 46 * H))

    stats = labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    assert stats["watches"] == 1
    row = db._conn.execute(
        "SELECT status, bars_truncated FROM outcomes WHERE kind='watch'"
    ).fetchone()
    assert row["status"] == "ok"
    assert row["bars_truncated"] == 1


def test_incomplete_watch_outcome_is_not_phase1_eligible(db):
    db.admit_control("tokC", "56", 48, _iso(ENTRY), admission_price_usd=1.0)
    db.insert_bars([
        {"token_address": "tokC", "network_id": "56", "resolution": "5",
         "ts": ENTRY + H, "o": 1.0, "h": 1.1, "l": 1.0, "c": 1.0,
         "h_suspect": 1, "fetched_at": "t"},
    ])
    db.set_bars_state("tokC", "56", "ok", 1, _iso(ENTRY + 48 * H + 60))

    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute(
        "SELECT status, analysis_eligible, exclusion_reason FROM outcomes"
    ).fetchone()
    assert row["status"] == "incomplete"
    assert row["analysis_eligible"] == 0
    assert row["exclusion_reason"] == "incomplete_metrics"


def test_mature_watch_waits_until_bars_fetch_is_finalized(db):
    db.admit_control("tokC", "56", 48, _iso(ENTRY), admission_price_usd=1.0)

    now = ENTRY + 49 * H
    assert labeler.label_pending(db, now_epoch=now)["watches"] == 0

    _seed_bars(db, "tokC", ENTRY)
    db.set_bars_state("tokC", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))
    assert labeler.label_pending(db, now_epoch=now)["watches"] == 1


def test_phase1_view_exposes_only_eligible_v2_watch_outcomes(db):
    db.admit_control("v2", "56", 48, _iso(ENTRY), admission_price_usd=1.0)
    _seed_bars(db, "v2", ENTRY)
    db.set_bars_state("v2", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))
    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    db._conn.execute(
        """INSERT INTO outcomes(
               kind,key,token_address,network_id,is_control,entry_ts,status,labeled_at,
               design_version,analysis_eligible,exclusion_reason)
           VALUES('watch','legacy','v1','56',1,?,'ok','t',1,0,
                  'legacy_control_design_v1')""",
        (ENTRY,),
    )

    rows = db._conn.execute("SELECT key FROM phase1_watch_outcomes").fetchall()
    assert [row["key"] for row in rows] == []


def test_phase1_view_exposes_only_v3_outcomes(db):
    db.upsert_static({
        "token_address": "v3", "network_id": "56", "recorded_at": _iso(ENTRY),
        "token_created_at": str(ENTRY - 3 * 86400), "raw_json": "{}",
    })
    db.admit_control(
        "v3", "56", 48, _iso(ENTRY), admission_price_usd=1.0,
        design_version=config.CONTROL_DESIGN_VERSION,
    )
    _seed_bars(db, "v3", ENTRY)
    db.set_bars_state("v3", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))
    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)
    rows = db._conn.execute("SELECT key FROM phase1_watch_outcomes").fetchall()
    assert [row["key"] for row in rows] == [f"v3:56:{_iso(ENTRY)}"]


def test_young_v3_watch_is_quarantined_at_label_time(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", _iso(ENTRY - H))
    db.upsert_static({
        "token_address": "young", "network_id": "56", "recorded_at": _iso(ENTRY),
        "token_created_at": str(ENTRY - 3600), "raw_json": "{}",
    })
    db.add_signal_comparison_window(
        "young", "56", "large_buy", "s-young", 48, _iso(ENTRY),
        1.0, config.CONTROL_DESIGN_VERSION, "verified",
    )
    _seed_bars(db, "young", ENTRY)
    db.set_bars_state("young", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute(
        "SELECT analysis_eligible, exclusion_reason FROM outcomes "
        "WHERE kind='watch' AND token_address='young'"
    ).fetchone()
    assert row["analysis_eligible"] == 0
    assert row["exclusion_reason"] == "age_gate_at_entry"


def test_v3_watch_with_only_post_entry_age_is_unknown_at_entry(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", _iso(ENTRY - H))
    db.upsert_static({
        "token_address": "late-age", "network_id": "56",
        "recorded_at": _iso(ENTRY + H),
        "token_created_at": str(ENTRY - 3 * 86400), "raw_json": "{}",
    })
    db.add_signal_comparison_window(
        "late-age", "56", "large_buy", "s-late-age", 48, _iso(ENTRY),
        1.0, config.CONTROL_DESIGN_VERSION, "verified",
    )
    _seed_bars(db, "late-age", ENTRY)
    db.set_bars_state("late-age", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute(
        "SELECT analysis_eligible, exclusion_reason FROM outcomes "
        "WHERE kind='watch' AND token_address='late-age'"
    ).fetchone()
    assert row["analysis_eligible"] == 0
    assert row["exclusion_reason"] == "age_unknown_at_entry"


def test_v3_future_creation_timestamp_is_unknown_not_age_gate(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", _iso(ENTRY - H))
    db.upsert_static({
        "token_address": "future-age", "network_id": "56",
        "recorded_at": _iso(ENTRY),
        "token_created_at": str(ENTRY + 86400), "raw_json": "{}",
    })
    db.add_signal_comparison_window(
        "future-age", "56", "large_buy", "s-future-age", 48, _iso(ENTRY),
        1.0, config.CONTROL_DESIGN_VERSION, "verified",
    )
    _seed_bars(db, "future-age", ENTRY)
    db.set_bars_state("future-age", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute(
        "SELECT analysis_eligible, exclusion_reason FROM outcomes "
        "WHERE kind='watch' AND token_address='future-age'"
    ).fetchone()
    assert row["analysis_eligible"] == 0
    assert row["exclusion_reason"] == "age_unknown_at_entry"


def test_age_filled_after_entry_is_not_treated_as_known_at_entry(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", _iso(ENTRY - H))
    db.upsert_static({
        "token_address": "filled-late", "network_id": "56",
        "recorded_at": _iso(ENTRY), "token_created_at": None, "raw_json": "{}",
    })
    db.add_signal_comparison_window(
        "filled-late", "56", "large_buy", "s-filled-late", 48, _iso(ENTRY),
        1.0, config.CONTROL_DESIGN_VERSION, "verified",
    )
    db.set_static_created_at(
        "filled-late", "56", str(ENTRY - 3 * 86400), _iso(ENTRY + H)
    )
    _seed_bars(db, "filled-late", ENTRY)
    db.set_bars_state("filled-late", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute(
        "SELECT analysis_eligible, exclusion_reason FROM outcomes "
        "WHERE kind='watch' AND token_address='filled-late'"
    ).fetchone()
    assert row["analysis_eligible"] == 0
    assert row["exclusion_reason"] == "age_unknown_at_entry"


def test_unproven_age_is_unknown_at_entry(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", _iso(ENTRY - H))
    db.upsert_static({
        "token_address": "unproven-age", "network_id": "56",
        "recorded_at": _iso(ENTRY),
        "token_created_at": str(ENTRY - 3 * 86400), "raw_json": "{}",
    })
    db._conn.execute(
        "UPDATE token_static SET token_created_at_observed_at=NULL "
        "WHERE token_address='unproven-age'"
    )
    db._conn.commit()
    db.add_signal_comparison_window(
        "unproven-age", "56", "large_buy", "s-unproven", 48, _iso(ENTRY),
        1.0, config.CONTROL_DESIGN_VERSION, "verified",
    )
    _seed_bars(db, "unproven-age", ENTRY)
    db.set_bars_state("unproven-age", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute(
        "SELECT analysis_eligible, exclusion_reason FROM outcomes "
        "WHERE kind='watch' AND token_address='unproven-age'"
    ).fetchone()
    assert row["analysis_eligible"] == 0
    assert row["exclusion_reason"] == "age_unknown_at_entry"


def test_v3_window_before_gate_is_not_retroactively_age_filtered(db, monkeypatch):
    monkeypatch.setattr(config, "AGE_GATE_ENABLED_AT", _iso(ENTRY + 10 * H))
    db.add_signal_comparison_window(
        "legacy-v3", "56", "large_buy", "s-legacy-v3", 48, _iso(ENTRY),
        1.0, config.CONTROL_DESIGN_VERSION, "verified",
    )
    _seed_bars(db, "legacy-v3", ENTRY)
    db.set_bars_state("legacy-v3", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    row = db._conn.execute(
        "SELECT analysis_eligible, exclusion_reason FROM outcomes "
        "WHERE kind='watch' AND token_address='legacy-v3'"
    ).fetchone()
    assert row["analysis_eligible"] == 1
    assert row["exclusion_reason"] is None


def test_signal_without_bars_gets_an_auditable_status_row(db):
    """Without bars the row is not silently dropped — status=no_entry stays
    auditable and is not re-examined every cycle forever."""
    _seed_signal(db, "s1", "ghost", _iso(ENTRY))
    stats = labeler.label_pending(db, now_epoch=ENTRY + 49 * H)
    assert stats["no_entry"] == 1
    row = db._conn.execute("SELECT status, entry_px FROM outcomes WHERE key='s1'").fetchone()
    assert row["status"] == "no_entry" and row["entry_px"] is None
    assert labeler.label_pending(db, now_epoch=ENTRY + 50 * H)["signals"] == 0


# ---------- Migration of the old outcomes table ----------

def test_old_empty_outcomes_table_is_replaced(tmp_path):
    import sqlite3

    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE outcomes (
            token_address TEXT NOT NULL, entry_ts TEXT NOT NULL,
            max_gain_1h REAL, PRIMARY KEY (token_address, entry_ts));
    """)
    c.commit()
    c.close()

    d = RecorderDB(p, SCHEMA)
    try:
        cols = {r["name"] for r in d._conn.execute("PRAGMA table_info(outcomes)")}
        assert "kind" in cols and "split" in cols
    finally:
        d.close()


def test_old_outcomes_with_data_is_preserved_as_legacy(tmp_path):
    import sqlite3

    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE outcomes (
            token_address TEXT NOT NULL, entry_ts TEXT NOT NULL,
            max_gain_1h REAL, PRIMARY KEY (token_address, entry_ts));
        INSERT INTO outcomes VALUES('tok','2026-01-01',1.5);
    """)
    c.commit()
    c.close()

    d = RecorderDB(p, SCHEMA)
    try:
        legacy = d._conn.execute("SELECT COUNT(*) FROM outcomes_legacy").fetchone()[0]
        assert legacy == 1                                   # the data was not destroyed
        cols = {r["name"] for r in d._conn.execute("PRAGMA table_info(outcomes)")}
        assert "kind" in cols
    finally:
        d.close()
