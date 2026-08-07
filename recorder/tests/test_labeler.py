"""اختبارات الموسِّم (بلا شبكة، بلا ساعة نظام).

تثبت خصائص عدم التسرّب قبل أي شيء: لا توسيم قبل نضج النافذة، سعر الدخول لا
يسبق الإشارة، القمم من الشموع التالية حصراً، والتقسيم بالعملة لا بالصفّ.
"""
import os

import pytest

import config
import labeler
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")

ENTRY = 1_785_000_000                      # لحظة الدخول (epoch)
H = 3600


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _bar(ts, o=1.0, h=1.0, low=1.0, c=1.0):
    return {"ts": ts, "o": o, "h": h, "l": low, "c": c}


def _series(*, entry_px=1.0):
    """سلسلة معلومة النتائج: دخول 1.0، قمّة 3.0 في الساعة 2، قاع 0.5 في
    الساعة 30، إغلاق نهائي 2.0."""
    bars = [_bar(ENTRY, c=entry_px)]                       # شمعة الدخول
    bars.append(_bar(ENTRY + 30 * 60, h=1.5, c=1.2))       # داخل أوّل ساعة
    bars.append(_bar(ENTRY + 2 * H, h=3.0, c=2.5))         # القمّة (ساعة 2)
    bars.append(_bar(ENTRY + 10 * H, h=2.0, c=1.8))
    bars.append(_bar(ENTRY + 30 * H, h=1.0, low=0.5, c=0.6))  # القاع
    bars.append(_bar(ENTRY + 48 * H, h=2.2, c=2.0))        # نهاية النافذة
    return bars


# ---------- compute_labels: دالة خالصة ----------

def test_labels_from_a_known_series():
    out = labeler.compute_labels(_series(), ENTRY)
    assert out["status"] == "ok"
    assert out["entry_px"] == 1.0
    assert out["entry_lag_s"] == 0
    assert out["max_gain_1h"] == pytest.approx(0.5)     # قمّة 1.5 خلال ساعة
    assert out["max_gain_4h"] == pytest.approx(2.0)     # قمّة 3.0 في الساعة 2
    assert out["max_gain_24h"] == pytest.approx(2.0)
    assert out["max_gain_48h"] == pytest.approx(2.0)
    assert out["max_drawdown_48h"] == pytest.approx(-0.5)
    assert out["final_return_48h"] == pytest.approx(1.0)
    assert out["time_to_peak_h"] == pytest.approx(2.0)
    assert out["candles_48h"] == 5
    assert out["bars_truncated"] == 0
    assert out["is_rug"] == 0


def test_entry_bar_never_precedes_the_signal():
    """تسرّب معكوس: شمعة قبل الإشارة لا تصلح دخولاً ولا تُحسب قممها."""
    bars = [_bar(ENTRY - 300, h=99.0, c=0.1)] + _series()
    out = labeler.compute_labels(bars, ENTRY)
    assert out["entry_px"] == 1.0                        # لا 0.1 السابقة
    assert out["max_gain_48h"] == pytest.approx(2.0)     # لا 99 السابقة


def test_entry_bars_own_high_is_not_counted_as_gain():
    """قمّة شمعة الدخول نفسها قد تسبق تنفيذنا — لا تُحسب مكسباً."""
    bars = [_bar(ENTRY, h=50.0, c=1.0), _bar(ENTRY + H, h=1.1, c=1.05)]
    out = labeler.compute_labels(bars, ENTRY)
    assert out["max_gain_48h"] == pytest.approx(0.1)     # لا x50


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
    """العملة الميّتة إشارة لا نقص — استبعادها يُدخل انحياز البقاء."""
    bars = [_bar(ENTRY, c=1.0), _bar(ENTRY + 2 * H, h=1.2, low=0.05, c=0.08)]
    out = labeler.compute_labels(bars, ENTRY)
    assert out["status"] == "ok"
    assert out["bars_truncated"] == 1
    assert out["last_bar_lag_h"] == pytest.approx(46.0)
    assert out["is_rug"] == 1                            # -92% ≤ عتبة -90%


def test_zero_entry_price_is_rejected_not_divided_by():
    bars = [_bar(ENTRY, c=0.0), _bar(ENTRY + H, c=1.0)]
    assert labeler.compute_labels(bars, ENTRY)["status"] == "no_entry"


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_admission_price_is_rejected(price):
    assert labeler.compute_labels(
        _series(), ENTRY, admission_price_usd=price
    )["status"] == "no_entry"


# ---------- التقسيم ----------

def test_split_is_deterministic_and_by_token():
    a = labeler.assign_split("0xAbCd")
    assert a == labeler.assign_split("0xabcd")           # حالة الأحرف لا تفرّق
    assert all(labeler.assign_split("0xAbCd") == a for _ in range(50))
    splits = {labeler.assign_split(f"tok{i}") for i in range(300)}
    assert splits == {"train", "val", "test"}            # الأقسام الثلاثة تظهر


# ---------- label_pending: تزايديّ وناضج فقط ----------

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
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_only_mature_windows_are_labeled(db):
    _seed_signal(db, "old", "tokA", _iso(ENTRY))
    _seed_signal(db, "fresh", "tokB", _iso(ENTRY + 40 * H))   # نافذتها لم تكتمل
    _seed_bars(db, "tokA", ENTRY)

    now = ENTRY + 48 * H + config.LABEL_MARGIN_SECONDS + 60
    stats = labeler.label_pending(db, now_epoch=now)

    assert stats["signals"] == 1
    keys = [r["key"] for r in db._conn.execute("SELECT key FROM outcomes WHERE kind='signal'")]
    assert keys == ["old"]                               # الطازجة تُركت لدورة لاحقة


def test_labeling_is_idempotent(db):
    _seed_signal(db, "s1", "tokA", _iso(ENTRY))
    _seed_bars(db, "tokA", ENTRY)
    now = ENTRY + 49 * H
    assert labeler.label_pending(db, now_epoch=now)["signals"] == 1
    assert labeler.label_pending(db, now_epoch=now)["signals"] == 0   # لا إعادة


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
    """69% من الإشارات متباعدة <5 دقائق — تكرار زائف يُعلَّم لا يُحذف."""
    _seed_signal(db, "first", "tokA", _iso(ENTRY))
    _seed_signal(db, "burst", "tokA", _iso(ENTRY + 600))       # بعد 10 دقائق
    _seed_signal(db, "later", "tokA", _iso(ENTRY + 2 * H))     # بعد ساعتين
    _seed_bars(db, "tokA", ENTRY)
    labeler.label_pending(db, now_epoch=ENTRY + 60 * H)

    flags = {r["key"]: r["is_independent"] for r in
             db._conn.execute("SELECT key, is_independent FROM outcomes WHERE kind='signal'")}
    assert flags == {"first": 1, "burst": 0, "later": 1}


def test_watch_entries_including_control_get_labeled(db):
    db.upsert_watch("tokA", "56", "large_buy", "s1", 48, _iso(ENTRY))
    db.admit_control("tokC", "56", 48, _iso(ENTRY))
    _seed_bars(db, "tokA", ENTRY)
    _seed_bars(db, "tokC", ENTRY, final=0.05)                  # الضابطة انهارت
    db.set_bars_state("tokA", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))
    db.set_bars_state("tokC", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))

    stats = labeler.label_pending(db, now_epoch=ENTRY + 49 * H)

    assert stats["watches"] == 2
    rows = {r["token_address"]: r for r in
            db._conn.execute("SELECT * FROM outcomes WHERE kind='watch'")}
    assert rows["tokA"]["is_control"] == 0
    assert rows["tokC"]["is_control"] == 1
    assert rows["tokC"]["is_rug"] == 1                         # -95%
    assert rows["tokC"]["is_independent"] is None              # لا يخصّ المراقبة
    assert rows["tokC"]["design_version"] == 2
    assert rows["tokC"]["analysis_eligible"] == 0
    assert rows["tokC"]["exclusion_reason"] == "superseded_comparison_design"


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
    db.admit_control(
        "v3", "56", 48, _iso(ENTRY), admission_price_usd=1.0,
        design_version=config.CONTROL_DESIGN_VERSION,
    )
    _seed_bars(db, "v3", ENTRY)
    db.set_bars_state("v3", "56", "ok", 3, _iso(ENTRY + 48 * H + 60))
    labeler.label_pending(db, now_epoch=ENTRY + 49 * H)
    rows = db._conn.execute("SELECT key FROM phase1_watch_outcomes").fetchall()
    assert [row["key"] for row in rows] == [f"v3:56:{_iso(ENTRY)}"]


def test_signal_without_bars_gets_an_auditable_status_row(db):
    """بلا شموع لا نُسقط الصفّ بصمت — status=no_entry يبقى قابلاً للجرد
    ولا يُعاد فحصه كل دورة إلى الأبد."""
    _seed_signal(db, "s1", "ghost", _iso(ENTRY))
    stats = labeler.label_pending(db, now_epoch=ENTRY + 49 * H)
    assert stats["no_entry"] == 1
    row = db._conn.execute("SELECT status, entry_px FROM outcomes WHERE key='s1'").fetchone()
    assert row["status"] == "no_entry" and row["entry_px"] is None
    assert labeler.label_pending(db, now_epoch=ENTRY + 50 * H)["signals"] == 0


# ---------- ترحيل outcomes القديم ----------

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
        assert legacy == 1                                   # البيانات لم تُتلف
        cols = {r["name"] for r in d._conn.execute("PRAGMA table_info(outcomes)")}
        assert "kind" in cols
    finally:
        d.close()
