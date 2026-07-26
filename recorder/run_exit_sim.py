"""تقرير مقارنة قواعد الخروج على الأرشيف الحالي.

    py run_exit_sim.py                    # المجموعة الافتراضية
    py run_exit_sim.py --cost 0.02        # مع تكلفة 2% لكل دورة
    py run_exit_sim.py --all-signals      # كل الإشارات لا أوّل إشارة لكل عملة

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
from exit_sim import DEFAULT_RULES, ExitRule, breakeven_cost, load_trades, simulate_all  # noqa: E402

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

    db = RecorderDB(config.DB_PATH, config.SCHEMA_PATH)
    try:
        trades = load_trades(db, independent_only=independent)
        if not trades:
            print("لا صفقات صالحة بعد — انتظر تراكم الشموع.")
            return
        holds = sorted(
            (t["bars"][-1]["ts"] - t["entry_ts"]) / 3600 for t in trades
        )
        print(f"صفقات: {len(trades)} · وسيط المتابعة المتاحة: {holds[len(holds) // 2]:.1f} ساعة")
        if holds[-1] < config.LABEL_WINDOW_HOURS:
            print(
                f"⚠ لا نافذة اكتملت بعد (أقصى متابعة {holds[-1]:.1f}س من "
                f"{config.LABEL_WINDOW_HOURS}س) — الأهداف البعيدة مُبخَّسة."
            )
        print(f"تكلفة الدورة المفترضة: {cost * 100:.1f}%\n")

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
