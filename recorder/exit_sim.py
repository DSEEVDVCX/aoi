"""Exit-rule simulator: what would you have made had you sold under a specific rule?

The idea it was born from: **we don't need the exact top**. Catching the top is
impossible to begin with, but a modest fixed achievable target is — and the
measurement on the archive proved it: holding to the end of the window gives a
**negative** median, while taking profit at +10% gives a **+10%** median.
The difference is not in which coins you pick but in **when you exit**.

## Why we walk the bars instead of using the outcomes columns

`outcomes` carries `max_gain_48h` and `max_drawdown_48h`, but **not their
order**. A coin that fell −40% then rose +50%: with a −30% stop you are out at
a loss, without a stop you are up. The two columns are identical in both cases.
**The path is the result**, so we walk the bars.

## The conservative assumption within a single bar

A bar gives `high` and `low` without their temporal order. If the target and the
stop are both touched within the same bar, there is no way to know which came
first, so we assume **the stop first** — the worse of the two possibilities.
This makes the results a lower bound, not an exaggeration
(`optimistic_same_candle=True` flips it to gauge the size of the effect, and is
not used for reporting).

## What this file does not simulate

It does not model slippage as a function of liquidity, nor execution rejection.
`cost` is a single parameter standing for the whole round trip (fees + slippage
both ways) — use `breakeven_cost` to find out how much the strategy can bear
before it turns losing. Measurement on the archive: breakeven at ~3%, and a
third of the coins have liquidity under $50k.
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
    """An exit rule. All thresholds are decimal fractions (0.20 = 20%).

    take_profit  — sell once this gain is reached.
    stop_loss    — sell once this drop is reached (a positive value means a negative threshold).
    trailing     — sell on a drop of this fraction **from the highest peak since entry**.
    time_limit_h — sell after this many hours whatever the price.
    cost         — the full round-trip cost (fees + slippage), subtracted from every trade.
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
            parts.append(f"take+{self.take_profit * 100:.0f}%")
        if self.stop_loss is not None:
            parts.append(f"stop-{self.stop_loss * 100:.0f}%")
        if self.trailing is not None:
            parts.append(f"trailing{self.trailing * 100:.0f}%")
        if self.time_limit_h is not None:
            parts.append(f"≤{self.time_limit_h:g}h")
        if self.cost:
            parts.append(f"cost{self.cost * 100:.1f}%")
        return " ".join(parts) or "hold"


def simulate_trade(
    bars: Sequence[dict[str, Any]],
    entry_ts: int,
    rule: ExitRule,
    optimistic_same_candle: bool = False,
) -> dict[str, Any] | None:
    """Simulates a single trade. Returns None if no valid entry can be determined.

    Entry: the close of the **first bar at/after** `entry_ts` (the labeler's own
    definition, and the same lag constraint) — so the simulation does not differ
    from labeling at the starting point.
    The exit is checked in the bars **strictly after the entry bar**: the entry
    bar's own high may have preceded our execution.
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

        # Conservative assumption: on a tie within the bar, the losing exit happens first.
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
        "net_return": gross - rule.cost,   # the cost is subtracted once per round trip
        "exit_reason": reason,
        "held_hours": hours,
    }


def load_trades(
    db: RecorderDB, independent_only: bool = True, source: str = "signal"
) -> list[dict[str, Any]]:
    """Loads entry points and their bars. By default only the first signal per coin.

    `independent_only` prevents pseudo-replication: 14.2 signals per coin with a
    median gap between them of 1.1 minutes — simulating them as separate trades
    inflates the result meaninglessly.

    `source`: "signal" (forward collection, signal_events) or "activity"
    (retro collection, activity_events with multi_user_buy events — the
    group-entry thesis only, neither sells nor theses). The same "first event
    per coin" logic in both.
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
    """Runs a rule over all the trades and summarizes. Always the mean **and** the
    median together: a single outlier winner flips the mean on its own (actually
    measured: +1092% in one coin)."""
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
    """The highest round-trip cost at which the rule stays profitable (by mean).

    The practically decisive number: a theoretical edge is meaningless if
    slippage eats it. Measurement on the archive gave ~3% — and a third of the
    coins have liquidity under $50k.
    """
    base = ExitRule(rule.take_profit, rule.stop_loss, rule.trailing, rule.time_limit_h, 0.0)
    gross = simulate_all(trades, base)
    if not gross.get("n") or gross["mean"] <= 0:
        return 0.0
    lo = 0.0
    for _ in range(50):  # binary search — the mean is linear in cost, but kept general
        mid = (lo + hi) / 2
        r = ExitRule(rule.take_profit, rule.stop_loss, rule.trailing, rule.time_limit_h, mid)
        if simulate_all(trades, r)["mean"] > 0:
            lo = mid
        else:
            hi = mid
    return lo


# A default rule set for quick comparison — includes hold as the baseline.
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
