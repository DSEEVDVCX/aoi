"""اختبارات محاكي قواعد الخروج (بلا شبكة، بلا قاعدة بيانات).

أهمّ ما تثبته: **المسار هو النتيجة**. عملة هبطت ثمّ صعدت تختلف نتيجتها عن عملة
صعدت ثمّ هبطت رغم تطابق `max_gain` و`max_drawdown` بينهما — وهذا بالضبط سبب
المشي على الشموع بدل قراءة أعمدة outcomes.
"""
import pytest
from exit_sim import ExitRule, breakeven_cost, simulate_all, simulate_trade

ENTRY = 1_785_000_000
H = 3600


def _b(ts, h=1.0, low=1.0, c=1.0):
    return {"ts": ts, "o": c, "h": h, "l": low, "c": c}


def _path(*steps):
    """أوّل شمعة هي الدخول عند 1.0، ثمّ (high, low, close) لكل ساعة."""
    bars = [_b(ENTRY, 1.0, 1.0, 1.0)]
    for i, (h, low, c) in enumerate(steps, start=1):
        bars.append(_b(ENTRY + i * H, h, low, c))
    return bars


# ---------- الأساسيات ----------

def test_target_exit_returns_exactly_the_target():
    bars = _path((1.5, 0.95, 1.4))
    r = simulate_trade(bars, ENTRY, ExitRule(take_profit=0.20))
    assert r["exit_reason"] == "target"
    assert r["gross_return"] == pytest.approx(0.20)   # لا 0.40 — نبيع عند الحدّ


def test_stop_exit_returns_exactly_the_stop():
    bars = _path((1.02, 0.5, 0.6))
    r = simulate_trade(bars, ENTRY, ExitRule(take_profit=0.20, stop_loss=0.30))
    assert r["exit_reason"] == "stop"
    assert r["gross_return"] == pytest.approx(-0.30)


def test_no_trigger_falls_back_to_window_end():
    bars = _path((1.05, 0.98, 1.03), (1.06, 0.99, 1.04))
    r = simulate_trade(bars, ENTRY, ExitRule(take_profit=0.50, stop_loss=0.50))
    assert r["exit_reason"] == "window_end"
    assert r["gross_return"] == pytest.approx(0.04)


def test_entry_bar_high_is_never_a_target_hit():
    """قمّة شمعة الدخول قد تسبق تنفيذنا — لا تُحسب خروجاً."""
    bars = [_b(ENTRY, h=99.0, low=1.0, c=1.0), _b(ENTRY + H, 1.05, 1.0, 1.02)]
    r = simulate_trade(bars, ENTRY, ExitRule(take_profit=0.20))
    assert r["exit_reason"] == "window_end"


def test_late_or_missing_entry_returns_none():
    assert simulate_trade([], ENTRY, ExitRule()) is None
    late = [_b(ENTRY + 4000, 1.0, 1.0, 1.0), _b(ENTRY + 8000, 1.5, 1.0, 1.4)]
    assert simulate_trade(late, ENTRY, ExitRule()) is None      # تأخّر >30 دقيقة
    zero = [_b(ENTRY, 1.0, 1.0, 0.0), _b(ENTRY + H, 2.0, 1.0, 2.0)]
    assert simulate_trade(zero, ENTRY, ExitRule()) is None      # سعر دخول صفريّ


# ---------- المسار هو النتيجة ----------

def test_same_extremes_opposite_order_give_opposite_results():
    """الاختبار الجوهريّ: `max_gain` و`max_drawdown` متطابقان في المسارين،
    والنتيجة معكوسة. لهذا لا يمكن الاكتفاء بأعمدة outcomes."""
    rule = ExitRule(take_profit=0.30, stop_loss=0.30)
    down_then_up = _path((1.0, 0.6, 0.7), (1.5, 0.7, 1.45))   # الوقف أوّلاً
    up_then_down = _path((1.5, 1.0, 1.45), (1.45, 0.6, 0.7))  # الهدف أوّلاً
    assert simulate_trade(down_then_up, ENTRY, rule)["gross_return"] == pytest.approx(-0.30)
    assert simulate_trade(up_then_down, ENTRY, rule)["gross_return"] == pytest.approx(0.30)


def test_same_candle_tie_assumes_the_loss_first():
    """داخل الشمعة لا ترتيب زمنيّ — نفترض الأسوأ فلا نبالغ في التقدير."""
    bars = _path((1.5, 0.6, 1.0))          # يلمس +50% و−40% معاً
    rule = ExitRule(take_profit=0.20, stop_loss=0.30)
    assert simulate_trade(bars, ENTRY, rule)["exit_reason"] == "stop"
    optimistic = simulate_trade(bars, ENTRY, rule, optimistic_same_candle=True)
    assert optimistic["exit_reason"] == "target"   # للقياس لا للتقرير


# ---------- الوقف المتحرّك والحدّ الزمنيّ ----------

def test_trailing_stop_exits_after_a_drop_from_the_peak():
    bars = _path((2.0, 1.0, 1.9), (1.9, 1.4, 1.45))   # قمّة 2.0 ثمّ هبوط 30%
    r = simulate_trade(bars, ENTRY, ExitRule(trailing=0.25))
    assert r["exit_reason"] == "trailing"
    assert r["gross_return"] == pytest.approx(0.5)    # 2.0 × 0.75 − 1


def test_trailing_does_not_trigger_before_any_rise():
    """بلا قمّة فوق الدخول لا معنى للوقف المتحرّك — وإلّا خرج فوراً دائماً."""
    bars = _path((1.0, 0.7, 0.75))
    r = simulate_trade(bars, ENTRY, ExitRule(trailing=0.25))
    assert r["exit_reason"] == "window_end"


def test_time_limit_exits_at_market():
    bars = _path((1.05, 0.98, 1.02), (1.06, 0.99, 1.03), (1.07, 1.0, 1.04))
    r = simulate_trade(bars, ENTRY, ExitRule(take_profit=0.9, time_limit_h=2))
    assert r["exit_reason"] == "time"
    assert r["gross_return"] == pytest.approx(0.03)
    assert r["held_hours"] == pytest.approx(2.0)


# ---------- التكلفة ----------

def test_cost_is_charged_once_per_round_trip():
    bars = _path((1.5, 1.0, 1.4))
    r = simulate_trade(bars, ENTRY, ExitRule(take_profit=0.20, cost=0.03))
    assert r["gross_return"] == pytest.approx(0.20)
    assert r["net_return"] == pytest.approx(0.17)


def test_cost_can_flip_a_winning_rule_to_losing():
    trades = [{"bars": _path((1.3, 0.95, 1.25)), "entry_ts": ENTRY} for _ in range(5)]
    assert simulate_all(trades, ExitRule(take_profit=0.10))["mean"] > 0
    assert simulate_all(trades, ExitRule(take_profit=0.10, cost=0.15))["mean"] < 0


def test_breakeven_cost_matches_the_gross_edge():
    trades = [{"bars": _path((1.3, 0.95, 1.25)), "entry_ts": ENTRY} for _ in range(5)]
    assert breakeven_cost(trades, ExitRule(take_profit=0.10)) == pytest.approx(0.10, abs=0.01)


def test_breakeven_is_zero_for_a_losing_rule():
    trades = [{"bars": _path((1.0, 0.5, 0.55)), "entry_ts": ENTRY} for _ in range(3)]
    assert breakeven_cost(trades, ExitRule(take_profit=0.50, stop_loss=0.30)) == 0.0


# ---------- التجميع ----------

def test_simulate_all_reports_median_beside_mean():
    """رابح شاذّ واحد يقلب المتوسّط وحده — الوسيط يكشف ذلك."""
    trades = [{"bars": _path((1.0, 0.85, 0.9)), "entry_ts": ENTRY} for _ in range(9)]
    trades.append({"bars": _path((30.0, 1.0, 29.0)), "entry_ts": ENTRY})
    r = simulate_all(trades, ExitRule())
    assert r["mean"] > 1.0        # يجرّه الشاذّ
    assert r["median"] < 0        # والوسيط يقول الحقيقة
    assert r["n"] == 10


def test_simulate_all_counts_exit_reasons_and_skips_invalid():
    trades = [
        {"bars": _path((1.5, 1.0, 1.4)), "entry_ts": ENTRY},   # هدف
        {"bars": _path((1.0, 0.5, 0.55)), "entry_ts": ENTRY},  # وقف
        {"bars": [], "entry_ts": ENTRY},                        # يُتخطّى
    ]
    r = simulate_all(trades, ExitRule(take_profit=0.20, stop_loss=0.30))
    assert r["n"] == 2
    assert r["exit_reasons"] == {"target": 1, "stop": 1}


def test_empty_input_is_reported_not_crashed():
    assert simulate_all([], ExitRule())["n"] == 0


def test_rule_labels_are_readable():
    assert ExitRule().label == "احتفاظ"
    assert "جني+20%" in ExitRule(take_profit=0.20).label
    assert "وقف-30%" in ExitRule(stop_loss=0.30).label
    assert "متحرّك25%" in ExitRule(trailing=0.25).label
