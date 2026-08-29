"""اختبارات الليبلين الجديدين: is_explosive و time_to_plus20_min (fv15)."""

import labeler


def _bars(closes: list[float], entry_ts: int = 1_787_600_000, step: int = 300):
    """شموع بسيطة: إغلاقات closes بعد نقطة الدخول، متباعدة step ثانية."""
    bars = []
    for i, c in enumerate(closes):
        ts = entry_ts + (i + 1) * step
        bars.append({"ts": ts, "o": c, "h": c, "l": c, "c": c, "v": 1.0,
                     "h_suspect": 0, "l_suspect": 0, "c_suspect": 0})
    return bars


def test_explosive_label_true_when_peak_and_speed_met():
    """قمة ≥+100% ونصفها (+50%) خلال أول 24س ⇒ is_explosive=1."""
    # 30 شمعة (2.5 ساعة): تصعد لـ+50% في أول 12 شمعة (1 ساعة) ثم القمة +120%
    closes = [1.0] * 2 + [1.5] * 10 + [2.2] * 18
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["is_explosive"] == 1


def test_explosive_false_when_peak_only_80pct():
    """قمة +80% (< 2x) ⇒ ليس انفجارًا حتى لو سريعة."""
    closes = [1.0] * 2 + [1.8] * 28
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["is_explosive"] == 0


def test_explosive_false_when_slow_explosion():
    """قمة +150% لكن +50% لم تتحقق خلال 24س (كلها في اليوم الثاني) ⇒ 0.

    الإشارة البطيئة صيدٌ هامشي (قياس 2026-08-28: متاحها خلال اليوم الأول
    +26% وسطيًا) — الليبل يفرّق «انفجار قابل للالتقاط» عن «قمة متأخرة».
    """
    # 48س = 576 شمعة. الأول 300 شمعة (25س) هادئة 1.0-1.1، آخرها تقفز لـ2.5
    closes = [1.0] * 300 + [1.1] * 200 + [2.5] * 76
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    # القمة 2.5x تحققت، لكن max_gain_24h = 1.1/1.0-1 = 10% < 50%
    assert out["max_gain_48h"] > 1.0
    assert out["is_explosive"] == 0


def test_time_to_plus20_measured_in_minutes():
    """time_to_plus20_min: دقائق حتى أول إغلاق ≥ +20% (فلتر إسقاط، لا دخول)."""
    # شمعة 4 (20 دقيقة) تصل 1.21
    closes = [1.0, 1.05, 1.1, 1.21, 1.3, 1.5]
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["time_to_plus20_min"] == 20


def test_time_to_plus20_null_when_never_reached():
    """لم تبلغ +20% إطلاقًا ⇒ NULL (غياب لا ما لا نهاية)."""
    closes = [1.0, 1.05, 1.1, 1.15, 1.18]
    out = labeler.compute_labels(bars=_bars(closes), entry_ts=1_787_600_000)
    assert out["time_to_plus20_min"] is None


def test_explosive_null_when_gains_missing():
    """نافذة بلا شموع كافية للحكم ⇒ الليبلان NULL (لا نفبرك حكمًا)."""
    out = labeler.compute_labels(bars=[], entry_ts=1_787_600_000)
    assert out["status"] != "ok"
    assert out["is_explosive"] is None
    assert out["time_to_plus20_min"] is None


def test_labels_not_in_feature_columns():
    """حارس التسريب: الليبلان في LABEL_COLUMNS لا FEATURE_COLUMNS أبدًا."""
    import features

    assert "is_explosive" in features.LABEL_COLUMNS
    assert "time_to_plus20_min" in features.LABEL_COLUMNS
    assert "is_explosive" not in features.FEATURE_COLUMNS
    assert "time_to_plus20_min" not in features.FEATURE_COLUMNS
