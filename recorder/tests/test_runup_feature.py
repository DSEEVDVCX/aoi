"""اختبارات ميزة pre_signal_runup (fv13) — موضع الصعود كقياس مستمر."""
import pytest
from db import RecorderDB

import features


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), "schema.sql")
    yield value
    value.close()


def _bars(db: RecorderDB, token: str, closes: list[float], *,
          end_ts: int = 1_787_600_000) -> None:
    """شموع مكتملة قبل end_ts، إغلاقها closes بالترتيب الزمني."""
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
    """صعود من أول شمعة في نافذة 24س إلى الإغلاق الأخير قبل t0."""
    # 24 شمعة من 1.0 إلى 2.0: runup = log(2.0/1.0)
    closes = [1.0 + i / 23 for i in range(24)]
    _bars(db, "RUNx", closes)
    out = features.price_history_features(db, "RUNx", "1399811149", T0)
    import math

    assert out["pre_signal_runup"] == pytest.approx(math.log(2.0), rel=1e-6)


def test_runup_uses_only_last_24h(db):
    """شموع أقدم من 24 ساعة لا تدخل البسط — النافذة 288 شمعة لا كل التاريخ."""
    # 300 شمعة (25 ساعة): القديمة جداً يجب أن تُستبعد من نافذة 24س
    closes = [10.0] * 12 + [1.0 + i / 275 for i in range(276)]
    _bars(db, "WINx", closes)
    out = features.price_history_features(db, "WINx", "1399811149", T0)
    import math

    # النافذة = آخر 288 شمعة: تبدأ من ~1.5 وليس 10.0
    window_first = closes[-288]
    expected = math.log(closes[-1] / window_first)
    assert out["pre_signal_runup"] == pytest.approx(expected, rel=1e-6)


def test_runup_null_when_insufficient_history(db):
    """أقل من 12 شمعة ⇒ NULL (غياب مقيس لا صفر)."""
    _bars(db, "NEWx", [1.0, 1.1, 1.2])
    out = features.price_history_features(db, "NEWx", "1399811149", T0)
    assert out["pre_signal_runup"] is None


def test_runup_zero_for_flat_history(db):
    """تاريخ كامل بلا حركة ⇒ 0.0 بالضبط (قياس لا غياب)."""
    _bars(db, "FLATx", [1.0] * 24)
    out = features.price_history_features(db, "FLATx", "1399811149", T0)
    assert out["pre_signal_runup"] == pytest.approx(0.0, abs=1e-9)


def test_feature_declared_in_columns():
    """العمود مواطن من الدرجة الأولى في FEATURE_COLUMNS وfv13."""
    assert "pre_signal_runup" in features.FEATURE_COLUMNS
    assert features.FEATURE_VERSION >= 13


def test_runup_never_uses_post_t0_bars(db):
    """قانون النقطة الزمنية: شموع بعد t0 لا تدخل الحساب إطلاقًا."""
    # 24 شمعة هادئة قبل t0 ثم 12 شمعة صاروخية بعده
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
