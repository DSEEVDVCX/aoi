"""محاكي قواعد الخروج: ماذا كنت ستجني لو بعت وفق قاعدة محدّدة؟

الفكرة التي وُلد منها: **لا نحتاج القمّة بالضبط**. اصطياد القمّة مستحيل سلفاً،
لكنّ هدفاً ثابتاً متواضعاً قابل للتحقيق — والقياس على الأرشيف أثبت ذلك: الاحتفاظ
حتى نهاية النافذة يعطي وسيطاً **سالباً**، بينما جني عند +10% يعطي وسيطاً **+10%**.
الفرق ليس في اختيار العملات بل في **متى تخرج**.

## لماذا نمشي على الشموع ولا نستعمل أعمدة outcomes

`outcomes` تحمل `max_gain_48h` و`max_drawdown_48h`، لكنّها **لا تحمل ترتيبهما**.
عملة هبطت −40% ثمّ صعدت +50%: مع وقف −30% أنت خارج بخسارة، وبلا وقف أنت رابح.
العمودان متطابقان في الحالتين. **المسار هو النتيجة**، فنمشي على الشموع.

## الافتراض المتحفّظ داخل الشمعة الواحدة

الشمعة تعطي `high` و`low` بلا ترتيبهما الزمني. إن لُمس الهدف والوقف في الشمعة
نفسها فلا سبيل لمعرفة الأسبق، فنفترض **الوقف أوّلاً** — أسوأ الاحتمالين. هذا
يجعل النتائج حدّاً أدنى لا مبالغة (`optimistic_same_candle=True` يقلبه لقياس
حجم الأثر، ولا يُستعمل للتقرير).

## ما لا يحاكيه هذا الملفّ

لا ينمذج الانزلاق حسب السيولة ولا رفض التنفيذ. `cost` معامل واحد يمثّل الدورة
كاملةً (رسوم + انزلاق ذهاباً وإياباً) — استعمل `breakeven_cost` لتعرف كم تحتمل
الاستراتيجية قبل أن تصير خاسرة. القياس على الأرشيف: التعادل عند ~3%، وثلث
العملات سيولتها دون 50 ألف دولار.
"""
from __future__ import annotations

import statistics as st
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import config
from db import RecorderDB


@dataclass(frozen=True)
class ExitRule:
    """قاعدة خروج. كل الحدود نِسَب عشرية (0.20 = 20%).

    take_profit  — بيع عند بلوغ هذا الارتفاع.
    stop_loss    — بيع عند بلوغ هذا الهبوط (قيمة موجبة تعني حدّاً سالباً).
    trailing     — بيع عند الهبوط بهذه النسبة **عن أعلى قمّة منذ الدخول**.
    time_limit_h — بيع بعد هذا العدد من الساعات مهما كان السعر.
    cost         — تكلفة الدورة كاملةً (رسوم + انزلاق)، تُطرح من كل صفقة.
    """

    take_profit: float | None = None
    stop_loss: float | None = None
    trailing: float | None = None
    time_limit_h: float | None = None
    cost: float = 0.0

    @property
    def label(self) -> str:
        parts = []
        if self.take_profit is not None:
            parts.append(f"جني+{self.take_profit * 100:.0f}%")
        if self.stop_loss is not None:
            parts.append(f"وقف-{self.stop_loss * 100:.0f}%")
        if self.trailing is not None:
            parts.append(f"متحرّك{self.trailing * 100:.0f}%")
        if self.time_limit_h is not None:
            parts.append(f"≤{self.time_limit_h:g}س")
        if self.cost:
            parts.append(f"تكلفة{self.cost * 100:.1f}%")
        return " ".join(parts) or "احتفاظ"


def simulate_trade(
    bars: Sequence[dict[str, Any]],
    entry_ts: int,
    rule: ExitRule,
    optimistic_same_candle: bool = False,
) -> dict[str, Any] | None:
    """يحاكي صفقة واحدة. يعيد None إن تعذّر تحديد دخول صالح.

    الدخول: إغلاق **أوّل شمعة عند/بعد** `entry_ts` (نفس تعريف الموسِّم، ونفس
    قيد التأخّر) — فلا تختلف المحاكاة عن التوسيم في نقطة البداية.
    الخروج يُفحص في الشموع **التالية بعد شمعة الدخول حصراً**: قمّة شمعة الدخول
    نفسها قد تكون سبقت تنفيذنا.
    """
    entry_bar = next((b for b in bars if b["ts"] >= entry_ts), None)
    if entry_bar is None:
        return None
    if entry_bar["ts"] - entry_ts > config.LABEL_ENTRY_MAX_LAG_SECONDS:
        return None
    entry = entry_bar["c"]
    if not entry or entry <= 0:
        return None

    peak = entry
    for b in bars:
        if b["ts"] <= entry_bar["ts"]:
            continue
        if b["h"] is None or b["l"] is None or b["c"] is None:
            continue
        hours = (b["ts"] - entry_ts) / 3600
        hit_tp = rule.take_profit is not None and b["h"] / entry - 1 >= rule.take_profit
        hit_sl = rule.stop_loss is not None and b["l"] / entry - 1 <= -rule.stop_loss
        hit_tr = (
            rule.trailing is not None
            and peak > entry
            and b["l"] / peak - 1 <= -rule.trailing
        )
        losing = hit_sl or hit_tr

        # الافتراض المتحفّظ: عند التعادل داخل الشمعة يقع الخروج الخاسر أوّلاً.
        order = ("tp", "loss") if (hit_tp and optimistic_same_candle) else ("loss", "tp")
        for which in order:
            if which == "loss" and losing:
                exit_r = -rule.stop_loss if hit_sl else peak / entry * (1 - rule.trailing) - 1
                return _result(entry, exit_r, "stop" if hit_sl else "trailing", hours, rule)
            if which == "tp" and hit_tp:
                return _result(entry, rule.take_profit, "target", hours, rule)

        if rule.time_limit_h is not None and hours >= rule.time_limit_h:
            return _result(entry, b["c"] / entry - 1, "time", hours, rule)
        peak = max(peak, b["h"])

    tail = [b for b in bars if b["ts"] > entry_bar["ts"] and b["c"] is not None]
    if not tail:
        return None
    last = tail[-1]
    return _result(
        entry, last["c"] / entry - 1, "window_end", (last["ts"] - entry_ts) / 3600, rule
    )


def _result(entry: float, gross: float, reason: str, hours: float, rule: ExitRule) -> dict[str, Any]:
    return {
        "entry_px": entry,
        "gross_return": gross,
        "net_return": gross - rule.cost,   # التكلفة تُطرح مرّة واحدة عن الدورة
        "exit_reason": reason,
        "held_hours": hours,
    }


def load_trades(
    db: RecorderDB, independent_only: bool = True, source: str = "signal"
) -> list[dict[str, Any]]:
    """يحمّل نقاط الدخول وشموعها. افتراضياً أوّل إشارة لكل عملة فقط.

    `independent_only` يمنع التكرار الزائف: 14.2 إشارة لكل عملة، ووسيط الفاصل
    بينها 1.1 دقيقة — محاكاتها كصفقات منفصلة تضخّم النتيجة بلا معنى.

    `source`: "signal" (الجمع الأماميّ، signal_events) أو "activity" (الرجعيّ،
    activity_events بأحداث multi_user_buy — أطروحة الدخول الجماعيّ فقط، لا
    البيع ولا الأطروحات). نفس منطق «أوّل حدث لكل عملة» في كليهما.
    """
    if source == "activity":
        sql = (
            """SELECT token_address a, network_id n,
                      CAST(strftime('%s', MIN(ts)) AS INTEGER) e
                 FROM activity_events
                WHERE ts IS NOT NULL AND token_address IS NOT NULL
                  AND event_type = 'multi_user_buy'
                GROUP BY 1, 2"""
            if independent_only
            else """SELECT token_address a, network_id n,
                           CAST(strftime('%s', ts) AS INTEGER) e
                      FROM activity_events
                     WHERE ts IS NOT NULL AND token_address IS NOT NULL
                       AND event_type = 'multi_user_buy'"""
        )
    else:
        sql = (
            """WITH f AS (SELECT token_address a, network_id n, MIN(ts) mts
                            FROM signal_events WHERE ts IS NOT NULL GROUP BY 1, 2)
                 SELECT a, n, CAST(strftime('%s', mts) AS INTEGER) e FROM f"""
            if independent_only
            else """SELECT token_address a, network_id n,
                           CAST(strftime('%s', ts) AS INTEGER) e
                      FROM signal_events WHERE ts IS NOT NULL"""
        )
    out = []
    for row in db._conn.execute(sql).fetchall():
        bars = db.bars_for(
            row["a"], str(row["n"] or ""), row["e"],
            row["e"] + config.LABEL_WINDOW_HOURS * 3600,
        )
        if len(bars) >= 2:
            out.append({"token_address": row["a"], "entry_ts": row["e"], "bars": bars})
    return out


def simulate_all(
    trades: Sequence[dict[str, Any]], rule: ExitRule, **kw: Any
) -> dict[str, Any]:
    """يشغّل قاعدة على كل الصفقات ويلخّص. المتوسّط **والوسيط** معاً دائماً:
    رابح شاذّ واحد يقلب المتوسّط وحده (قيس فعلاً: +1092% في عملة واحدة)."""
    rows = [
        r for r in (simulate_trade(t["bars"], t["entry_ts"], rule, **kw) for t in trades)
        if r is not None
    ]
    if not rows:
        return {"rule": rule.label, "n": 0}
    nets = [r["net_return"] for r in rows]
    wins = [x for x in nets if x > 0]
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[r["exit_reason"]] = reasons.get(r["exit_reason"], 0) + 1
    return {
        "rule": rule.label,
        "n": len(rows),
        "mean": st.mean(nets),
        "median": st.median(nets),
        "win_rate": len(wins) / len(nets),
        "worst": min(nets),
        "best": max(nets),
        "median_hold_h": st.median([r["held_hours"] for r in rows]),
        "exit_reasons": reasons,
    }


def breakeven_cost(
    trades: Sequence[dict[str, Any]], rule: ExitRule, hi: float = 0.5
) -> float:
    """أقصى تكلفة دورة تبقى معها القاعدة رابحة (بالمتوسّط).

    الرقم الحاسم عملياً: الأفضلية النظرية بلا معنى إن ابتلعها الانزلاق. القياس
    على الأرشيف أعطى ~3% — وثلث العملات سيولتها دون 50 ألف دولار.
    """
    base = ExitRule(rule.take_profit, rule.stop_loss, rule.trailing, rule.time_limit_h, 0.0)
    gross = simulate_all(trades, base)
    if not gross.get("n") or gross["mean"] <= 0:
        return 0.0
    lo = 0.0
    for _ in range(50):  # بحث ثنائيّ — المتوسّط خطّيّ في التكلفة لكن نبقيه عامّاً
        mid = (lo + hi) / 2
        r = ExitRule(rule.take_profit, rule.stop_loss, rule.trailing, rule.time_limit_h, mid)
        if simulate_all(trades, r)["mean"] > 0:
            lo = mid
        else:
            hi = mid
    return lo


# مجموعة قواعد افتراضية للمقارنة السريعة — تشمل الاحتفاظ كخطّ أساس.
DEFAULT_RULES: tuple[ExitRule, ...] = (
    ExitRule(),
    ExitRule(take_profit=0.10),
    ExitRule(take_profit=0.20),
    ExitRule(take_profit=0.50),
    ExitRule(take_profit=0.10, stop_loss=0.30),
    ExitRule(take_profit=0.20, stop_loss=0.30),
    ExitRule(take_profit=0.30, stop_loss=0.30),
    ExitRule(take_profit=0.50, stop_loss=0.30),
    ExitRule(trailing=0.25),
    ExitRule(take_profit=0.20, stop_loss=0.30, time_limit_h=12),
)
