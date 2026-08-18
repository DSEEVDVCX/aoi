"""تقرير مقارنة قواعد الخروج على الأرشيف الحالي.

    py run_exit_sim.py                        # المجموعة الافتراضية (الجمع الأماميّ)
    py run_exit_sim.py --cost 0.02            # مع تكلفة 2% لكل دورة
    py run_exit_sim.py --all-signals          # كل الإشارات لا أوّل إشارة لكل عملة
    py run_exit_sim.py --source activity      # الرجعيّ: multi_user_buy من activity_events

يقرأ فقط؛ آمن مع المسجّل العامل.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
from db import RecorderDB  # noqa: E402
from exit_sim import (  # noqa: E402
    DEFAULT_RULES,
    ExitRule,
    breakeven_cost,
    load_trades,
    simulate_all,
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass


def _farg(name: str, default: float) -> float:
    if name in sys.argv:
        try:
            return float(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def main() -> None:
    cost = _farg("--cost", 0.0)
    independent = "--all-signals" not in sys.argv
    source = "activity" if "--source" in sys.argv and "activity" in sys.argv else "signal"

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        trades = load_trades(db, independent_only=independent, source=source)
        if not trades:
            print("لا صفقات صالحة بعد — انتظر تراكم الشموع.")
            return
        holds = sorted(
            (t["bars"][-1]["ts"] - t["entry_ts"]) / 3600 for t in trades
        )
        src_label = ("الرجعيّ: multi_user_buy من activity_events (بلا ضابطة ممكنة)"
                     if source == "activity" else "الجمع الأماميّ: signal_events")
        print(f"المصدر: {src_label}")
        print(f"صفقات: {len(trades)} · وسيط المتابعة المتاحة: {holds[len(holds) // 2]:.1f} ساعة")

        # حالة النضج تُطبع **قبل** الأرقام لا بعدها: الجدول أدناه استطلاعيّ ما
        # لم تكتمل النوافذ وتنضج الضابطة، ولا يجوز أن يُقرأ كنتيجة.
        # عتبة المتابعة المكتملة = النافذة ناقص شمعة واحدة: آخر شمعة 5د تسبق
        # نهاية النافذة بدقائق، فالفحص الحرفي (h>=48) يسقطها كلّها كذباً.
        mature = sum(1 for h in holds if h >= config.LABEL_WINDOW_HOURS - 1)
        db_controls = db._conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE is_control=1 AND active=1"
        ).fetchone()[0]
        blockers = []
        if mature == 0:
            blockers.append(
                f"لا نافذة اكتملت (أقصى متابعة {holds[-1]:.1f}س من "
                f"{config.LABEL_WINDOW_HOURS}س) ⇒ الأهداف البعيدة مُبخَّسة بنيوياً"
            )
        if len(trades) < 1000:
            blockers.append(f"العيّنة {len(trades)} صفقة، دون عتبة النضج (1000)")
        blockers.append(
            f"لا مقارنة ضابطة في هذا التقرير ({db_controls} عملة ضابطة متاحة) "
            "⇒ الفرق قد يكون خاصية سوق لا استراتيجية"
        )
        print()
        print("┌─ استطلاعيّ — لا تُبنَ عليه قرارات " + "─" * 36)
        for b in blockers:
            print(f"│ • {b}")
        print("└" + "─" * 70)
        print(f"\nتكلفة الدورة المفترضة: {cost * 100:.1f}%\n")

        hdr = f"{'القاعدة':<34}{'متوسّط':>9}{'وسيط':>9}{'فوز':>8}{'أسوأ':>10}{'حيازة':>8}"
        print(hdr)
        print("-" * len(hdr))
        for rule in DEFAULT_RULES:
            r = simulate_all(
                trades,
                ExitRule(rule.take_profit, rule.stop_loss, rule.trailing,
                         rule.time_limit_h, cost),
            )
            if not r["n"]:
                continue
            print(
                f"{r['rule']:<34}{r['mean'] * 100:>8.2f}%{r['median'] * 100:>8.2f}%"
                f"{r['win_rate'] * 100:>7.1f}%{r['worst'] * 100:>9.1f}%"
                f"{r['median_hold_h']:>7.1f}س"
            )

        print("\n=== أقصى تكلفة تحتملها كل قاعدة قبل أن تصير خاسرة ===")
        for rule in DEFAULT_RULES:
            be = breakeven_cost(trades, rule)
            flag = "" if be > 0.03 else "  ← أضيق من انزلاق واقعيّ"
            print(f"  {rule.label:<34}{be * 100:>6.2f}%{flag}")
        print(
            "\nثلث العملات سيولتها دون 50 ألف دولار؛ الانزلاق وحده قد يبتلع"
            "\nالأفضلية كاملةً. القاعدة التي تعادلها دون ~3% ليست قابلة للتنفيذ عملياً."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
