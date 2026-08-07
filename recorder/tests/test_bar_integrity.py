"""اختبارات سلامة الشموع: التشوّه القادم من المنبع.

الحادثتان المرجعيّتان (2026-07-30، مؤكَّدتان بإعادة سحب حيّة):
1. `h` مستحيل: 2,626,092.02 لشمعة إغلاقها 0.021948 (×119 مليون) ⇒ اللوحة عرضت
   +62,570,743,609%.
2. الإغلاق نفسه مشوّه: 12,052.5 بين إغلاقين ≈0.0004 ⇒ `max_gain` = +2.1 مليار%.

المبدأ المختبَر: **السعر متّصل** — قفزة لا تستمرّ إلى الجار تشوّهٌ لا سعر.
"""
import extract
from labeler import compute_labels

FETCHED = "2026-07-30T10:35:02+00:00"


def _env(bars):
    """(ts, o, h, l, c) → مغلّف getBarsNew بمصفوفات متوازية."""
    return {"responseObject": {
        "s": "ok",
        "t": [b[0] for b in bars], "o": [b[1] for b in bars],
        "h": [b[2] for b in bars], "l": [b[3] for b in bars],
        "c": [b[4] for b in bars], "v": [1.0] * len(bars),
    }}


def _series(*rows):
    """(o, h, l, c) لكلٍّ → سلسلة مرتّبة بأختام كل 5 دقائق."""
    return [
        {"ts": 1785394200 + i * 300, "o": o, "h": h, "l": low, "c": c}
        for i, (o, h, low, c) in enumerate(rows)
    ]


# --- الفحص المعزول (شمعة بلا جيران) ---
def test_isolated_real_candle_is_not_flagged():
    assert extract.bar_wick_flags(0.0040, 0.0045, 0.0039, 0.0040) == (0, 0)


def test_isolated_big_but_plausible_wick_is_kept():
    """×5 ذيل حقيقيّ — العتبة ×10 مقيسة على 651 ألف شمعة."""
    assert extract.bar_wick_flags(0.001, 0.005, 0.0009, 0.001) == (0, 0)


def test_isolated_impossible_high_is_flagged():
    assert extract.bar_wick_flags(0.00403, 2626092.02, 0.00394, 0.02194)[0] == 1


def test_isolated_impossible_low_is_flagged():
    assert extract.bar_wick_flags(0.01, 0.011, 1e-9, 0.01)[1] == 1


def test_isolated_missing_or_zero_never_flags_nor_divides_by_zero():
    assert extract.bar_wick_flags(None, None, None, None) == (0, 0)
    assert extract.bar_wick_flags(0.0, 1.0, 0.0, 0.0) == (0, 0)


# --- الحكم بالجار: الحالتان الحقيقيّتان ---
def test_context_flags_impossible_high_case():
    """h = 2,626,092 بإغلاق سليم 0.0219 وجيران 0.0040/0.0202."""
    flags = extract.bar_context_flags(_series(
        (0.00402, 0.00403, 0.00401, 0.00403),
        (0.00403, 2626092.02, 0.00394, 0.02194),
        (0.02194, 0.02261, 0.01721, 0.02026),
    ))
    assert flags[1] == (1, 0, 0)          # القمّة وحدها مشوّهة
    assert flags[0] == (0, 0, 0) and flags[2] == (0, 0, 0)


def test_context_flags_corrupt_close_case():
    """الإغلاق نفسه 12,052.5 بين 0.000358 و0.000395 — لا يستمرّ ⇒ مشوّه."""
    flags = extract.bar_context_flags(_series(
        (0.000501, 0.000501, 0.000358, 0.000358),
        (0.000358, 12052.5, 0.000301, 12052.5),
        (12052.5, 12052.5, 0.000394, 0.000394),
    ))
    assert flags[1] == (1, 0, 1)          # الإغلاق والقمّة معاً
    # الشمعة التالية إغلاقها سليم، لكن قمّتها ورثت القيمة الفاسدة ⇒ تُعلَّم
    assert flags[2][0] == 1 and flags[2][2] == 0


def test_context_keeps_real_persistent_pump():
    """قفزة ×5.4 **استمرّت** (MarsCoin الحقيقية) — سعر لا تشوّه."""
    flags = extract.bar_context_flags(_series(
        (0.00402, 0.00403, 0.00401, 0.00403),
        (0.00403, 0.02200, 0.00394, 0.02194),
        (0.02194, 0.02261, 0.01721, 0.02026),
    ))
    assert all(f == (0, 0, 0) for f in flags)


def test_context_keeps_real_rug_dump():
    """هبوط 99% **استمرّ** = rug حقيقيّ يجب أن يبقى ليبلاً صحيحاً."""
    flags = extract.bar_context_flags(_series(
        (0.010, 0.011, 0.0099, 0.010),
        (0.010, 0.010, 0.00009, 0.0001),
        (0.0001, 0.00011, 0.00009, 0.0001),
    ))
    assert all(f == (0, 0, 0) for f in flags)


def test_context_flags_non_persistent_dip_in_close():
    flags = extract.bar_context_flags(_series(
        (0.010, 0.011, 0.0099, 0.010),
        (0.010, 0.010, 1e-9, 1e-9),        # إغلاق مجهريّ يعود فوراً
        (1e-9, 0.011, 0.0099, 0.010),
    ))
    assert flags[1][2] == 1


def test_context_single_bar_falls_back_to_isolated_check():
    flags = extract.bar_context_flags(_series((0.004, 2626092.02, 0.0039, 0.0219)))
    assert flags[0][0] == 1


# --- الاستخراج يحمل الأعلام ويحفظ الخام ---
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
    assert rows[1]["h"] == 2626092.022316815   # الخام كما ورد — لا يُصلَح ولا يُحذف
    assert rows[1]["c"] == 0.021948
    assert rows[0]["h_suspect"] == 0 and rows[2]["h_suspect"] == 0


# --- الموسِّم لا يُسمَّم ---
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
    assert out["max_gain_48h"] == 0.0230 / 0.0042 - 1     # لا مليارات
    assert out["suspect_bars"] == 1
    assert out["time_to_peak_h"] == 900 / 3600


def test_labels_ignore_suspect_close_in_final_return():
    bars = [
        _bar(ENTRY, 0.010, 0.010),
        _bar(ENTRY + 300, 0.011, 0.011),
        _bar(ENTRY + 600, 12052.5, 12052.5, hs=1, cs=1),   # آخر شمعة مشوّهة كليّاً
    ]
    out = compute_labels(bars, ENTRY)
    assert out["final_return_48h"] == 0.011 / 0.010 - 1    # من آخر إغلاق سليم
    assert out["is_rug"] == 0


def test_labels_skip_suspect_entry_close():
    """إغلاق الدخول مشوّه ⇒ نتخطّاه إلى أوّل سليم بدل مقام فاسد."""
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
    assert out["max_drawdown_48h"] == 0.008 / 0.01 - 1     # لا -100%
    assert out["suspect_bars"] == 1


def test_all_highs_suspect_leaves_gain_none_not_fabricated():
    bars = [_bar(ENTRY, 0.01, 0.01), _bar(ENTRY + 300, 9e9, 0.01, hs=1)]
    out = compute_labels(bars, ENTRY)
    assert out["max_gain_48h"] is None
    assert out["time_to_peak_h"] is None
    assert out["final_return_48h"] == 0.0


def test_clean_bars_unchanged_by_the_fix():
    """شموع بلا أعلام: نفس النتائج السابقة تماماً (لا انحدار)."""
    bars = [
        {"ts": ENTRY, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0},
        {"ts": ENTRY + 300, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5},
    ]
    out = compute_labels(bars, ENTRY)
    assert out["max_gain_48h"] == 1.0
    assert out["max_drawdown_48h"] == -0.5
    assert out["final_return_48h"] == 0.5
    assert out["suspect_bars"] == 0
