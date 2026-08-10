"""اختبارات فلتر الميزات الميتة وعلامات الحِقبة في أنبوب التدريب."""
import numpy as np
import pandas as pd

from train_pipeline import MIN_HALF_FOR_EPOCH, prune_dead_features

N = MIN_HALF_FOR_EPOCH * 2  # نصفٌ كافٍ لتفعيل كشف الحِقبة


def _frame(**cols):
    return pd.DataFrame({k: pd.Series(v, dtype=float) for k, v in cols.items()})


def test_constant_column_dropped():
    """`social_replies` يصل دائماً 0 — لا معلومة فيه."""
    f = _frame(social_replies=[0.0] * N, real=np.arange(N, dtype=float))
    assert prune_dead_features(f, ["social_replies", "real"]) == ["real"]


def test_all_null_column_dropped():
    f = _frame(dead=[np.nan] * N, real=np.arange(N, dtype=float))
    assert prune_dead_features(f, ["dead", "real"]) == ["real"]


def test_varying_column_kept():
    f = _frame(real=np.arange(N, dtype=float))
    assert prune_dead_features(f, ["real"]) == ["real"]


def test_column_that_starts_working_is_flagged_as_epoch():
    """لو أصلحت المنصّة حقلاً ميتاً: قديمه 0 وجديده متغيّر ⇒ ساعة لا ميزة."""
    col = [0.0] * (N // 2) + list(np.arange(1, N // 2 + 1, dtype=float))
    assert prune_dead_features(_frame(fixed=col), ["fixed"]) == []


def test_new_source_null_in_past_is_flagged_as_epoch():
    """مصدر بدأ اليوم: NULL في كل الماضي — نفس فخّ `LIVE_START_TS`."""
    col = [np.nan] * (N // 2) + list(np.arange(N // 2, dtype=float))
    assert prune_dead_features(_frame(holders=col), ["holders"]) == []


def test_epoch_column_kept_once_value_appears_in_both_halves():
    """يعود تلقائياً حين تغطّي قيمُه النصفين — لا حاجة لتحرير يدوي."""
    col = [0.0] * (N // 2) + [0.0, 5.0] * (N // 4)
    assert prune_dead_features(_frame(fixed=col), ["fixed"]) == ["fixed"]


def test_small_frame_uses_variance_filter_only():
    """تحت الحدّ لا نميّز الحِقبة من الصدفة، فنُبقي المتغيّر ونُسقط الثابت."""
    n = MIN_HALF_FOR_EPOCH  # نصفه = 100 < الحدّ
    col = [0.0] * (n // 2) + list(np.arange(1, n // 2 + 1, dtype=float))
    f = _frame(fixed=col, flat=[1.0] * n)
    assert prune_dead_features(f, ["fixed", "flat"]) == ["fixed"]


def test_late_half_constant_also_flagged():
    """الاتجاه المعاكس: مصدر توقّف عن الوصول."""
    col = list(np.arange(1, N // 2 + 1, dtype=float)) + [np.nan] * (N // 2)
    assert prune_dead_features(_frame(stopped=col), ["stopped"]) == []
