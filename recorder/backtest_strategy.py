"""A full backtest of the derived strategy over the whole archive (train + val + test).

The strategy (derived from pattern analysis, thresholds from train only):
    1. Entry: a live independent signal on a meme coin with:
         - strong momentum:   ret_24h_before  >  the 80th percentile (q80)
         - real volatility:   vol_24h_before  >  the 60th percentile (q60)
         - a young coin:      token_age_h     <  the 25th percentile (q25)
    2. Exit: target +20% · stop −30% · time limit 24 hours · round-trip cost 2%.

Always compared against the baseline (all signals) and against the opposite
face (signals the pattern does not pick), so market drift is not read as a
strategic edge. The simulation walks the bars with the same rules as
`exit_sim` (the stop before the target within a single bar).

    py backtest_strategy.py                     # the whole archive
    py backtest_strategy.py --cost 0.02         # a custom cost
    py backtest_strategy.py --no-sim            # the columns, without bar simulation

Read-only; safe alongside the running recorder and labeler.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from db import RecorderDB  # noqa: E402
from exit_sim import ExitRule, simulate_trade  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover
        pass

COST = 0.02
TP = 0.20
SL = 0.30
TIME_LIMIT_H = 24


def _flag(name: str) -> bool:
    return name in sys.argv


def _farg(name: str, default: float) -> float:
    if name in sys.argv:
        try:
            return float(sys.argv[sys.argv.index(name) + 1])
        except (IndexError, ValueError):
            pass
    return default


def load_frame(db_path: str) -> pd.DataFrame:
    uri = os.path.abspath(db_path).replace("\\", "/")
    conn = __import__("sqlite3").connect(f"file:{uri}?mode=ro", uri=True)
    try:
        frame = pd.read_sql_query(
            "SELECT * FROM model_training_rows WHERE suspect_bars = 0", conn
        )
    finally:
        conn.close()
    if frame.empty:
        raise RuntimeError("model_training_rows has no eligible rows")
    return frame


def strategy_mask(frame: pd.DataFrame, train: pd.DataFrame) -> np.ndarray:
    q = lambda col, p: float(train[col].quantile(p))  # noqa: E731
    ret, vol, age = q("ret_24h_before", 0.80), q("vol_24h_before", 0.60), q("token_age_h", 0.25)
    return (
        (frame["ret_24h_before"] > ret)
        & (frame["vol_24h_before"] > vol)
        & (frame["token_age_h"] < age)
    ).to_numpy(dtype=bool)


def simulate_all_rows(
    db_path: str, frame: pd.DataFrame, rule: ExitRule, selected: np.ndarray
) -> list[dict]:
    db = RecorderDB(db_path, config.SCHEMA_PATH)
    rows = []
    try:
        for i, row in enumerate(frame.itertuples(index=False)):
            sim = simulate_trade(
                db.bars_for(
                    row.token_address,
                    str(row.network_id or ""),
                    int(row.entry_ts),
                    int(row.entry_ts) + config.LABEL_WINDOW_HOURS * 3600,
                ),
                int(row.entry_ts),
                rule,
            )
            if sim is None:
                continue
            rows.append(
                {
                    "token_address": row.token_address,
                    "network_id": row.network_id,
                    "split": row.split,
                    "entry_ts": int(row.entry_ts),
                    "selected": bool(selected[i]),
                    **sim,
                }
            )
    finally:
        db.close()
    return rows


def summarize(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "n": 0}
    nets = np.asarray([r["net_return"] for r in rows], dtype=float)
    days = sorted({pd.to_datetime(r["entry_ts"], unit="s", utc=True).date() for r in rows})
    by_day = pd.Series(
        {d: sum(r["net_return"] for r in rows if pd.to_datetime(r["entry_ts"], unit="s", utc=True).date() == d) for d in days}
    )
    equity = by_day.cumsum()
    peak = equity.cummax()
    drawdown = (equity - peak).min()
    return {
        "label": label,
        "n": len(rows),
        "tokens": len({(r["token_address"], r["network_id"]) for r in rows}),
        "mean": float(nets.mean()),
        "median": float(np.median(nets)),
        "win_rate": float((nets > 0).mean()),
        "best": float(nets.max()),
        "worst": float(nets.min()),
        "profit_factor": float(nets[nets > 0].sum() / max(1e-9, -nets[nets <= 0].sum())),
        "total_net": float(nets.sum()),
        "days": len(days),
        "max_dd": float(drawdown),
        "reasons": {
            r: sum(1 for x in rows if x["exit_reason"] == r)
            for r in ("target", "stop", "time", "window_end")
        },
        "median_hold_h": float(np.median([r["held_hours"] for r in rows])),
    }


def fmt_pct(v: float, signed: bool = False) -> str:
    return f"{v * 100:{'+' if signed else ''}.2f}%"


def main() -> None:
    db_path = config.DB_PATH
    cost = _farg("--cost", COST)
    do_sim = not _flag("--no-sim")

    frame = load_frame(db_path)
    train = frame.loc[frame["split"] == "train"]
    sel = strategy_mask(frame, train)
    print(f"Archive: {len(frame):,} independent signals · {frame.groupby(['token_address', 'network_id']).ngroups} coins")
    print(f"Strategy selects: {int(sel.sum()):,} signals ({sel.mean():.1%}) — thresholds from train only\n")

    if not do_sim:
        for split in ("train", "val", "test"):
            part = frame.loc[frame["split"] == split]
            selp = strategy_mask(part, train)
            print(f"{split:<6} n={len(part):5d} selected={int(selp.sum()):4d} "
                  f"up20={part['max_gain_24h'].ge(0.20).mean():.1%}")
        return

    rule = ExitRule(take_profit=TP, stop_loss=SL, time_limit_h=TIME_LIMIT_H, cost=cost)
    rows = simulate_all_rows(db_path, frame, rule, sel)
    print(f"Simulated successfully: {len(rows):,} trades · exit rule: {rule.label}\n")

    all_rows = rows
    strat = [r for r in rows if r["selected"]]
    anti = [r for r in rows if not r["selected"]]

    print("=== Results table (all data splits together) ===")
    print(f"{'group':<28}{'n':>6}{'mean':>9}{'median':>9}{'win':>8}{'best':>9}{'worst':>9}{'profit factor':>10}")
    for name, s in (("all signals", all_rows), ("strategy", strat), ("anti", anti)):
        r = summarize(s, name)
        if not r["n"]:
            continue
        print(f"{name:<28}{r['n']:>6}{fmt_pct(r['mean'], True):>9}{fmt_pct(r['median'], True):>9}"
              f"{r['win_rate'] * 100:>7.1f}%{fmt_pct(r['best'], True):>9}{fmt_pct(r['worst'], True):>9}"
              f"{r['profit_factor']:>10.2f}")
    print("\n(All splits together means: the older days are train + the newer val/test — the difference between the splits below matters more)")

    print("\n=== By time split ===")
    print(f"{'split':<8}{'group':<14}{'n':>5}{'mean':>9}{'median':>9}{'win':>8}{'target':>7}{'stop':>7}{'profit factor':>10}")
    for split in ("train", "val", "test"):
        for label, src in (("all", all_rows), ("strategy", strat), ("anti", anti)):
            r = summarize([x for x in src if x["split"] == split], label)
            if not r["n"]:
                continue
            reasons = r["reasons"]
            print(f"{split:<8}{label:<14}{r['n']:>5}{fmt_pct(r['mean'], True):>9}{fmt_pct(r['median'], True):>9}"
                  f"{r['win_rate'] * 100:>7.1f}%{reasons['target'] / r['n'] * 100:>6.1f}%"
                  f"{reasons['stop'] / r['n'] * 100:>6.1f}%{r['profit_factor']:>10.2f}")

    print("\n=== $100 per trade portfolio (strategy over the whole archive) ===")
    s = summarize(strat, "strategy")
    per = 100.0
    pnl = s["total_net"] * per
    invested = s["n"] * per
    print(f"Trades: {s['n']} · coins: {s['tokens']} · days: {s['days']} ({s['n'] / s['days']:.1f} trades/day)")
    print(f"Invested: ${invested:,.0f} ($100 per trade)")
    print(f"Total net profit: ${pnl:+,.0f} ({fmt_pct(s['total_net'])} on the capital deployed)")
    print(f"Mean per trade: ${s['mean'] * per:+.2f} · median: ${s['median'] * per:+.2f}")
    print(f"Worst daily streak (equity drawdown): {fmt_pct(s['max_dd'], True)}")
    print(f"Profit factor: {s['profit_factor']:.2f} (above 1 = gains cover losses)")

    print("\n=== Serial reinvestment of the same $100 (in chronological order) ===")
    ordered = sorted(strat, key=lambda r: r["entry_ts"])
    equity = 100.0
    curve = [(ordered[0]["entry_ts"], 100.0)]
    for r in ordered:
        equity *= 1 + r["net_return"]
        curve.append((r["entry_ts"], equity))
    print(f"Final balance: ${equity:,.2f} (from $100 across {len(ordered)} consecutive trades)")
    eq = np.asarray([c[1] for c in curve])
    peak = np.maximum.accumulate(eq)
    dd = ((eq - peak) / peak).min()
    print(f"Max equity drawdown: {fmt_pct(dd, True)}")

    print("\n=== Cost robustness (the highest round-trip cost before losing) ===")
    for label, src in (("all signals", all_rows), ("strategy", strat), ("anti", anti)):
        be = float(np.mean([r["gross_return"] for r in src]))
        flag = "" if be > 0.03 else "  ← tighter than realistic slippage"
        print(f"  {label:<24} breakeven ≈ {fmt_pct(be)}{flag}")

    print("\n=== Exit reason distribution (strategy) ===")
    reasons = s["reasons"]
    total = sum(reasons.values())
    for k in ("target", "stop", "time", "window_end"):
        print(f"  {k:<12} {reasons[k]:>5} ({reasons[k] / total:.1%})")

    print("\nThis is a historical backtest on data we collected; it is not equivalent "
          "to real execution (slippage, execution rejection, and trade concurrency "
          "are not represented).")


if __name__ == "__main__":
    main()
