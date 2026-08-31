"""Report comparing exit rules over the current archive.

    py run_exit_sim.py                        # the default set (forward collection)
    py run_exit_sim.py --cost 0.02            # with a 2% cost per round trip
    py run_exit_sim.py --all-signals          # every signal, not the first per coin
    py run_exit_sim.py --source activity      # retro: multi_user_buy from activity_events

Read-only; safe alongside the running recorder.
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
            print("No valid trades yet — wait for bars to accumulate.")
            return
        holds = sorted(
            (t["bars"][-1]["ts"] - t["entry_ts"]) / 3600 for t in trades
        )
        src_label = ("retro: multi_user_buy from activity_events (no control arm possible)"
                     if source == "activity" else "forward collection: signal_events")
        print(f"Source: {src_label}")
        print(f"Trades: {len(trades)} · median follow-up available: {holds[len(holds) // 2]:.1f} hours")

        # Maturity status is printed **before** the numbers, not after: the table
        # below is exploratory until the windows complete and the control arm
        # matures, and must not be read as a result.
        # The completed-follow-up threshold = the window minus one bar: the last
        # 5m bar precedes the end of the window by minutes, so a literal check
        # (h>=48) would falsely drop them all.
        mature = sum(1 for h in holds if h >= config.LABEL_WINDOW_HOURS - 1)
        db_controls = db._conn.execute(
            "SELECT COUNT(*) FROM watchlist WHERE is_control=1 AND active=1"
        ).fetchone()[0]
        blockers = []
        if mature == 0:
            blockers.append(
                f"no completed window (max follow-up {holds[-1]:.1f}h of "
                f"{config.LABEL_WINDOW_HOURS}h) ⇒ far targets are structurally understated"
            )
        if len(trades) < 1000:
            blockers.append(f"the sample is {len(trades)} trades, below the maturity threshold (1000)")
        blockers.append(
            f"no control comparison in this report ({db_controls} control coins available) "
            "⇒ the difference may be a market property, not a strategy"
        )
        print()
        print("┌─ Exploratory — do not build decisions on it " + "─" * 36)
        for b in blockers:
            print(f"│ • {b}")
        print("└" + "─" * 70)
        print(f"\nAssumed round-trip cost: {cost * 100:.1f}%\n")

        hdr = f"{'rule':<34}{'mean':>9}{'median':>9}{'win':>8}{'worst':>10}{'hold':>8}"
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
                f"{r['median_hold_h']:>7.1f}h"
            )

        print("\n=== The maximum cost each rule can bear before turning losing ===")
        for rule in DEFAULT_RULES:
            be = breakeven_cost(trades, rule)
            flag = "" if be > 0.03 else "  ← tighter than realistic slippage"
            print(f"  {rule.label:<34}{be * 100:>6.2f}%{flag}")
        print(
            "\nA third of the coins have liquidity under $50k; slippage alone can"
            "\neat the entire edge. A rule that breaks even below ~3% is not"
            "\npractically executable."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
