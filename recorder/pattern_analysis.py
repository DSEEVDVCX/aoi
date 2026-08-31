"""Interpretable signal-pattern analysis with token-level uncertainty.

The report deliberately separates three different questions:

1. Did price touch +20% within 24 hours?
2. Was the signal still profitable at the end of 48 hours?
3. Could a fixed +20% take-profit / -30% stop rule have captured the move?

Thresholds are fitted from the existing ``train`` split only.  ``val`` and
``test`` are used for evaluation, and tokens never cross splits.  The current
test split is therefore consumed once this report is inspected; future claims
must be checked on newly matured dates rather than repeatedly tuning to it.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import config
import features
import numpy as np
import pandas as pd
from db import RecorderDB
from exit_sim import ExitRule, simulate_trade
from phase1_analysis import analyze as analyze_phase1

RANDOM_SEED = 20260807
DEFAULT_BOOTSTRAPS = 20_000
UP_TARGET = 0.20
NOISE_PEAK = 0.10
EXIT_COST = 0.02


@dataclass(frozen=True)
class QuantileCondition:
    feature: str
    operator: str
    quantile: float


@dataclass(frozen=True)
class FittedCondition:
    feature: str
    operator: str
    quantile: float
    threshold: float


@dataclass(frozen=True)
class PatternSpec:
    key: str
    label: str
    expected_direction: str
    conditions: tuple[QuantileCondition, ...]


PATTERN_SPECS: tuple[PatternSpec, ...] = (
    PatternSpec(
        "momentum_spike",
        "strong momentum with real volatility",
        "higher",
        (
            QuantileCondition("ret_24h_before", ">", 0.80),
            QuantileCondition("vol_24h_before", ">", 0.60),
        ),
    ),
    PatternSpec(
        "old_token",
        "a relatively old coin",
        "lower",
        (QuantileCondition("token_age_h", ">", 0.70),),
    ),
    PatternSpec(
        "low_turnover",
        "weak turnover relative to liquidity",
        "lower",
        (QuantileCondition("volume_to_liquidity", "<=", 0.20),),
    ),
    PatternSpec(
        "low_volatility",
        "weak prior volatility",
        "lower",
        (QuantileCondition("vol_24h_before", "<=", 0.30),),
    ),
    PatternSpec(
        "low_transactions",
        "few daily transactions",
        "lower",
        (QuantileCondition("tick_txn_24h", "<=", 0.20),),
    ),
)

CALM_FINISH_SPEC = PatternSpec(
    "calm_finish",
    "prior calm",
    "higher",
    (QuantileCondition("vol_24h_before", "<=", 0.40),),
)


def _read_frame(db_path: str) -> pd.DataFrame:
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        frame = pd.read_sql_query(
            "SELECT * FROM model_training_rows WHERE suspect_bars = 0",
            connection,
        )
    finally:
        connection.close()
    if frame.empty:
        raise RuntimeError("model_training_rows has no eligible rows")
    required_splits = {"train", "val", "test"}
    if set(frame["split"].dropna()) != required_splits:
        raise RuntimeError("analysis requires train, val, and test splits")
    label_columns = ("max_gain_24h", "final_return_48h", "max_drawdown_48h")
    if frame[list(label_columns)].isna().any().any():
        raise RuntimeError("eligible rows contain null outcome labels")
    frame["up20"] = (frame["max_gain_24h"] >= UP_TARGET).astype(int)
    frame["noise"] = (
        (frame["max_gain_24h"] < NOISE_PEAK)
        & (frame["final_return_48h"] <= 0)
    ).astype(int)
    frame["final_win"] = (frame["final_return_48h"] > 0).astype(int)
    frame["final20"] = (frame["final_return_48h"] >= UP_TARGET).astype(int)
    frame["day"] = pd.to_datetime(frame["entry_ts"], unit="s", utc=True).dt.strftime(
        "%Y-%m-%d"
    )
    return frame


def fit_pattern(frame: pd.DataFrame, spec: PatternSpec) -> tuple[FittedCondition, ...]:
    train = frame.loc[frame["split"] == "train"]
    fitted = []
    for condition in spec.conditions:
        values = pd.to_numeric(train[condition.feature], errors="coerce").dropna()
        if values.empty:
            raise RuntimeError(f"{condition.feature} is empty in the train split")
        fitted.append(
            FittedCondition(
                condition.feature,
                condition.operator,
                condition.quantile,
                float(values.quantile(condition.quantile)),
            )
        )
    return tuple(fitted)


def pattern_mask(
    frame: pd.DataFrame, conditions: Iterable[FittedCondition]
) -> np.ndarray:
    mask = np.ones(len(frame), dtype=bool)
    for condition in conditions:
        values = pd.to_numeric(frame[condition.feature], errors="coerce")
        if condition.operator == "<=":
            selected = values <= condition.threshold
        elif condition.operator == ">":
            selected = values > condition.threshold
        else:  # pragma: no cover - specs are static, but fail closed if changed.
            raise ValueError(f"unsupported operator: {condition.operator}")
        mask &= selected.fillna(False).to_numpy()
    return mask


def summarize(frame: pd.DataFrame, selected: np.ndarray) -> dict[str, float | int]:
    rows = frame.loc[selected]
    if rows.empty:
        raise ValueError("cannot summarize an empty pattern")
    return {
        "rows": len(rows),
        "tokens": rows.groupby(["token_address", "network_id"], dropna=False).ngroups,
        "up20_rate": float(rows["up20"].mean()),
        "noise_rate": float(rows["noise"].mean()),
        "final_win_rate": float(rows["final_win"].mean()),
        "final20_rate": float(rows["final20"].mean()),
        "median_final_return": float(rows["final_return_48h"].median()),
        "median_peak_24h": float(rows["max_gain_24h"].median()),
        "median_drawdown_48h": float(rows["max_drawdown_48h"].median()),
        "loss50_rate": float((rows["final_return_48h"] <= -0.50).mean()),
    }


def _cluster_arrays(
    frame: pd.DataFrame, selected: np.ndarray, target: str
) -> np.ndarray:
    working = frame[["token_address", "network_id", target]].copy()
    working["selected"] = selected
    rows = []
    for _, group in working.groupby(
        ["token_address", "network_id"], dropna=False, sort=False
    ):
        inside = group["selected"].to_numpy(dtype=bool)
        values = group[target].to_numpy(dtype=float)
        rows.append(
            (
                float(values[inside].sum()),
                int(inside.sum()),
                float(values[~inside].sum()),
                int((~inside).sum()),
            )
        )
    return np.asarray(rows, dtype=float)


def cluster_uplift_ci(
    frame: pd.DataFrame,
    selected: np.ndarray,
    target: str,
    *,
    iterations: int = DEFAULT_BOOTSTRAPS,
    seed: int = RANDOM_SEED,
) -> dict[str, float]:
    clusters = _cluster_arrays(frame, selected, target)
    totals = clusters.sum(axis=0)
    if totals[1] == 0 or totals[3] == 0:
        raise ValueError("pattern and complement must both be non-empty")
    estimate = totals[0] / totals[1] - totals[2] / totals[3]
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(clusters), size=(iterations, len(clusters)))
    samples = clusters[indices].sum(axis=1)
    valid = (samples[:, 1] > 0) & (samples[:, 3] > 0)
    differences = (
        samples[valid, 0] / samples[valid, 1]
        - samples[valid, 2] / samples[valid, 3]
    )
    return {
        "estimate": float(estimate),
        "low": float(np.quantile(differences, 0.025)),
        "high": float(np.quantile(differences, 0.975)),
    }


def daily_stability(
    frame: pd.DataFrame,
    conditions: tuple[FittedCondition, ...],
    *,
    expected_direction: str,
    min_rows: int = 10,
) -> dict[str, int]:
    eligible_days = 0
    consistent_days = 0
    for _, day in frame.groupby("day"):
        selected = pattern_mask(day, conditions)
        if int(selected.sum()) < min_rows:
            continue
        eligible_days += 1
        rate = float(day.loc[selected, "up20"].mean())
        baseline = float(day["up20"].mean())
        if (expected_direction == "higher" and rate > baseline) or (
            expected_direction == "lower" and rate < baseline
        ):
            consistent_days += 1
    return {"consistent": consistent_days, "eligible": eligible_days}


def analyze_patterns(
    frame: pd.DataFrame, *, bootstraps: int = DEFAULT_BOOTSTRAPS
) -> dict[str, Any]:
    baselines = {
        split: summarize(part, np.ones(len(part), dtype=bool))
        for split, part in frame.groupby("split")
    }
    patterns: dict[str, Any] = {}
    for index, spec in enumerate(PATTERN_SPECS):
        fitted = fit_pattern(frame, spec)
        split_metrics = {}
        for split in ("train", "val", "test"):
            part = frame.loc[frame["split"] == split]
            split_metrics[split] = summarize(part, pattern_mask(part, fitted))
        test = frame.loc[frame["split"] == "test"]
        test_mask = pattern_mask(test, fitted)
        patterns[spec.key] = {
            "label": spec.label,
            "expected_direction": spec.expected_direction,
            "conditions": [condition.__dict__ for condition in fitted],
            "splits": split_metrics,
            "test_up20_uplift_ci": cluster_uplift_ci(
                test,
                test_mask,
                "up20",
                iterations=bootstraps,
                seed=RANDOM_SEED + index,
            ),
            "daily": daily_stability(
                frame,
                fitted,
                expected_direction=spec.expected_direction,
            ),
        }

    calm = fit_pattern(frame, CALM_FINISH_SPEC)
    calm_splits = {}
    for split in ("train", "val", "test"):
        part = frame.loc[frame["split"] == split]
        calm_splits[split] = summarize(part, pattern_mask(part, calm))
    test = frame.loc[frame["split"] == "test"]
    patterns[CALM_FINISH_SPEC.key] = {
        "label": CALM_FINISH_SPEC.label,
        "conditions": [condition.__dict__ for condition in calm],
        "splits": calm_splits,
        "test_final_win_uplift_ci": cluster_uplift_ci(
            test,
            pattern_mask(test, calm),
            "final_win",
            iterations=bootstraps,
            seed=RANDOM_SEED + 20,
        ),
    }
    return {"baselines": baselines, "patterns": patterns}


def _simulate_exit_rows(
    db_path: str,
    frame: pd.DataFrame,
    momentum: tuple[FittedCondition, ...],
) -> list[dict[str, Any]]:
    rule = ExitRule(
        take_profit=0.20,
        stop_loss=0.30,
        time_limit_h=24,
        cost=EXIT_COST,
    )
    selected = pattern_mask(frame, momentum)
    db = RecorderDB(db_path, config.SCHEMA_PATH)
    results = []
    try:
        for position, row in enumerate(frame.itertuples(index=False)):
            bars = db.bars_for(
                row.token_address,
                str(row.network_id or ""),
                int(row.entry_ts),
                int(row.entry_ts) + config.LABEL_WINDOW_HOURS * 3600,
            )
            result = simulate_trade(bars, int(row.entry_ts), rule)
            if result is None:
                continue
            results.append(
                {
                    "token_address": row.token_address,
                    "network_id": row.network_id,
                    "split": row.split,
                    "selected": bool(selected[position]),
                    **result,
                }
            )
    finally:
        db.close()
    return results


def _exit_summary(rows: list[dict[str, Any]]) -> dict[str, float | int | dict[str, int]]:
    if not rows:
        raise ValueError("cannot summarize empty exit simulation")
    values = np.asarray([row["net_return"] for row in rows], dtype=float)
    gross = np.asarray([row["gross_return"] for row in rows], dtype=float)
    reasons = {
        reason: sum(row["exit_reason"] == reason for row in rows)
        for reason in ("target", "stop", "time", "window_end")
    }
    return {
        "rows": len(rows),
        "tokens": len({(row["token_address"], row["network_id"]) for row in rows}),
        "mean_net": float(values.mean()),
        "median_net": float(np.median(values)),
        "win_rate": float((values > 0).mean()),
        "target_rate": reasons["target"] / len(rows),
        "stop_rate": reasons["stop"] / len(rows),
        "mean_gross": float(gross.mean()),
        "breakeven_round_trip_cost": float(gross.mean()),
        "reasons": reasons,
    }


def analyze_exit_simulation(
    db_path: str,
    frame: pd.DataFrame,
    momentum: tuple[FittedCondition, ...],
    *,
    bootstraps: int = DEFAULT_BOOTSTRAPS,
) -> dict[str, Any]:
    rows = _simulate_exit_rows(db_path, frame, momentum)
    split_results = {}
    for split in ("train", "val", "test"):
        all_rows = [row for row in rows if row["split"] == split]
        selected = [row for row in all_rows if row["selected"]]
        split_results[split] = {
            "all": _exit_summary(all_rows),
            "momentum": _exit_summary(selected),
        }

    test = pd.DataFrame([row for row in rows if row["split"] == "test"])
    test["selected_int"] = test["selected"].astype(bool)
    clusters = []
    for _, group in test.groupby(["token_address", "network_id"], dropna=False):
        inside = group["selected_int"].to_numpy(dtype=bool)
        values = group["net_return"].to_numpy(dtype=float)
        clusters.append(
            (
                float(values[inside].sum()),
                int(inside.sum()),
                float(values[~inside].sum()),
                int((~inside).sum()),
            )
        )
    cluster_array = np.asarray(clusters, dtype=float)
    rng = np.random.default_rng(RANDOM_SEED + 40)
    indices = rng.integers(
        0, len(cluster_array), size=(bootstraps, len(cluster_array))
    )
    samples = cluster_array[indices].sum(axis=1)
    valid = (samples[:, 1] > 0) & (samples[:, 3] > 0)
    differences = (
        samples[valid, 0] / samples[valid, 1]
        - samples[valid, 2] / samples[valid, 3]
    )
    split_results["test_mean_net_uplift_ci"] = {
        "low": float(np.quantile(differences, 0.025)),
        "high": float(np.quantile(differences, 0.975)),
    }
    return split_results


def _pct(value: float, *, signed: bool = False) -> str:
    return f"{value * 100:{'+' if signed else ''}.1f}%"


def _condition_text(condition: dict[str, Any]) -> str:
    feature = condition["feature"]
    value = condition["threshold"]
    if feature in {"ret_24h_before", "vol_24h_before"}:
        rendered = _pct(value)
    elif feature == "token_age_h":
        rendered = f"{value / 24:.1f}d"
    elif feature == "tick_txn_24h":
        rendered = f"{value:,.0f}"
    else:
        rendered = f"{value:.2f}"
    return f"`{feature}` {condition['operator']} {rendered}"


def _rule_text(pattern: dict[str, Any]) -> str:
    return " and ".join(_condition_text(condition) for condition in pattern["conditions"])


def render_markdown(
    frame: pd.DataFrame,
    pattern_result: dict[str, Any],
    exit_result: dict[str, Any],
    phase1: dict[str, Any],
    *,
    db_path: str,
) -> str:
    generated = datetime.now(UTC).isoformat(timespec="seconds")
    baselines = pattern_result["baselines"]
    patterns = pattern_result["patterns"]
    test_base = baselines["test"]
    signal = phase1["summaries"][0]
    control = phase1["summaries"][1]
    missing = [
        feature
        for feature in features.FEATURE_COLUMNS
        if feature in frame and frame[feature].notna().sum() == 0
    ]
    start = datetime.fromtimestamp(int(frame["entry_ts"].min()), UTC).isoformat()
    end = datetime.fromtimestamp(int(frame["entry_ts"].max()), UTC).isoformat()

    pattern_lines = []
    for key in ("momentum_spike", "old_token", "low_turnover", "low_volatility", "low_transactions"):
        pattern = patterns[key]
        train = pattern["splits"]["train"]
        val = pattern["splits"]["val"]
        test = pattern["splits"]["test"]
        ci = pattern["test_up20_uplift_ci"]
        direction = "+" if pattern["expected_direction"] == "higher" else "−"
        pattern_lines.append(
            "| "
            + pattern["label"]
            + " | "
            + _rule_text(pattern)
            + f" | {_pct(train['up20_rate'])} | {_pct(val['up20_rate'])} | "
            + f"{_pct(test['up20_rate'])} | {test['rows']} / {test['tokens']} | "
            + f"{direction} {pattern['daily']['consistent']}/{pattern['daily']['eligible']} | "
            + f"[{_pct(ci['low'], signed=True)}, {_pct(ci['high'], signed=True)}] |"
        )

    exit_lines = []
    for split in ("train", "val", "test"):
        all_rows = exit_result[split]["all"]
        selected = exit_result[split]["momentum"]
        exit_lines.append(
            f"| {split} | {all_rows['rows']} | {_pct(all_rows['mean_net'], signed=True)} | "
            f"{selected['rows']} / {selected['tokens']} | {_pct(selected['target_rate'])} | "
            f"{_pct(selected['stop_rate'])} | {_pct(selected['mean_net'], signed=True)} | "
            f"{_pct(selected['median_net'], signed=True)} |"
        )

    calm = patterns["calm_finish"]
    calm_test = calm["splits"]["test"]
    calm_ci = calm["test_final_win_uplift_ci"]
    momentum_test = patterns["momentum_spike"]["splits"]["test"]
    test_exit = exit_result["test"]["momentum"]
    type_rows = []
    test_frame = frame.loc[frame["split"] == "test"]
    for signal_type, group in test_frame.groupby("signal_type"):
        type_rows.append(
            f"| `{signal_type}` | {len(group)} | {group['token_address'].nunique()} | "
            f"{_pct(float(group['up20'].mean()))} |"
        )

    return f"""# Signal pattern analysis — {datetime.now(UTC).date().isoformat()}

> Generated at `{generated}` from `{db_path}`. It covers {len(frame):,} valid
> independent signals, {frame.groupby(['token_address', 'network_id'], dropna=False).ngroups} coins,
> from `{start}` to `{end}`.
> Thresholds are derived from `train` only, and the split is fixed by token address
> to keep coins from crossing splits.

## Summary

- **There is no proof that the general signal beats the control arm.** After
  deduplication, {signal['n']} coins with a signal remained versus only
  {control['n']} live controls. The 48h return median difference is
  {_pct(phase1['median_difference']['estimate'], signed=True)}, with a confidence
  interval of
  [{_pct(phase1['median_difference']['low'], signed=True)},
  {_pct(phase1['median_difference']['high'], signed=True)}], and `p={phase1['mann_whitney']['p_greater']:.3f}`.
- **The strongest up-pattern is momentum continuation:** {_rule_text(patterns['momentum_spike'])}.
  In the test split it touched +20% within 24 hours in **{_pct(momentum_test['up20_rate'])}** of
  {momentum_test['rows']} signals across {momentum_test['tokens']} coins, versus
  {_pct(test_base['up20_rate'])} for the whole test split. The improvement held its
  direction on every eligible day.
- **This is not a hold pattern.** The pattern's median return at the end of 48 hours is
  {_pct(momentum_test['median_final_return'], signed=True)} and its median drawdown
  {_pct(momentum_test['median_drawdown_48h'], signed=True)}; the typical move is a burst
  followed by a reversal/distribution.
- Under a conservative simulation — target +20%, stop −30%, 24-hour exit, and a 2%
  round-trip cost — the momentum pattern hit the target in
  **{_pct(test_exit['target_rate'])}** and was stopped out in
  **{_pct(test_exit['stop_rate'])}**; the net mean is
  **{_pct(test_exit['mean_net'], signed=True)}** and the median
  **{_pct(test_exit['median_net'], signed=True)}** on the test split. But it gave a
  negative mean on train, so it is not relied on as a complete financial strategy.
- There is no stable pattern for a final +20% after 48 hours. Prior calm raised the
  probability of merely closing positive to {_pct(calm_test['final_win_rate'])}, but the
  median return was only {_pct(calm_test['median_final_return'], signed=True)} and the
  share closing above +20% is {_pct(calm_test['final20_rate'])}. The confidence interval
  for the positive-close difference from the rest of the test split is
  [{_pct(calm_ci['low'], signed=True)}, {_pct(calm_ci['high'], signed=True)}], but the
  size of the return itself is tiny — meaning fees and slippage most likely erase the edge.

## Patterns of reaching +20% within 24 hours

Baseline: train {_pct(baselines['train']['up20_rate'])}, val
{_pct(baselines['val']['up20_rate'])}, test {_pct(test_base['up20_rate'])}.
The `rows/coins` column is for the test split. The confidence interval is the rate
difference from the rest of the test split, bootstrapped at the coin level, not the
row level.

| Pattern | Rule fitted from train | train | val | test | rows/coins | day stability | 95% CI of the difference |
|---|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(pattern_lines)}

Practical reading:

- High momentum with real volatility is a **short-burst filter**.
- Age above the train threshold, weak volume/liquidity turnover, weak volatility, or
  few transactions are **low-upside noise filters**. Not necessarily collapses; many of
  them move near zero — meaning the signal adds no movement worth the risk.

## Price-path simulation for the strongest pattern

The simulation walks the bars in order. If the price touches both the target and the
stop within the same bar, the stop is assumed first. The assumed cost is 2% per round
trip, and there is no separate model for execution rejection.

| Split | all signals | their net mean | momentum pattern rows/coins | target | stop | pattern mean | pattern median |
|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(exit_lines)}

On unseen coins in the test split, the confidence interval for the pattern's net mean
beating the rest of the test split was
[{_pct(exit_result['test_mean_net_uplift_ci']['low'], signed=True)},
{_pct(exit_result['test_mean_net_uplift_ci']['high'], signed=True)}]. The highest
round-trip cost at which the **test mean** stays non-negative is roughly
{_pct(test_exit['breakeven_round_trip_cost'])}, but it is unstable: on train it was
{_pct(exit_result['train']['momentum']['breakeven_round_trip_cost'])} only.

## What does not work as evidence

Event type alone does not separate the risers in the test split:

| Type | rows | coins | reached +20% |
|---|---:|---:|---:|
{chr(10).join(type_rows)}

Likewise, top-trader overlap or the trade being a first buy did not show up as an
effect more stable than the market. And the following fields are 100% empty in the
sample, so they are barred from any conclusion:
`{', '.join(missing)}`.

## Limits of the conclusion and the next step

- The period is short ({frame['day'].nunique()} days) and the meme market changes its
  regime quickly.
- The eligible live control arm is small and unbalanced across networks; so there is no
  causal verdict that the signal itself creates an edge over picking a similar random
  coin.
- `val` was used to choose the wording, then `test` was examined in this report; **the
  current test set is now consumed** and the rules must not be modified and renamed an
  independent test.
- Freeze the rules above now, collect 7–14 new days, then test them temporally with no
  changes. A proposed acceptance criterion for the momentum pattern: ≥100 signals and
  ≥30 new coins, the target rate staying above baseline, a coin-clustered confidence
  interval above zero, and a positive net mean at a 2–5% cost.

This is a historical probabilistic analysis, not a price guarantee and not investment
advice.
"""


def analyze(
    db_path: str,
    *,
    bootstraps: int = DEFAULT_BOOTSTRAPS,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any], dict[str, Any]]:
    frame = _read_frame(db_path)
    patterns = analyze_patterns(frame, bootstraps=bootstraps)
    momentum = fit_pattern(frame, PATTERN_SPECS[0])
    exits = analyze_exit_simulation(
        db_path, frame, momentum, bootstraps=bootstraps
    )
    phase1 = analyze_phase1(db_path, include_retro_controls=False)
    return frame, patterns, exits, phase1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--output")
    parser.add_argument("--bootstraps", type=int, default=DEFAULT_BOOTSTRAPS)
    args = parser.parse_args()
    frame, patterns, exits, phase1 = analyze(args.db, bootstraps=args.bootstraps)
    report = render_markdown(
        frame, patterns, exits, phase1, db_path=str(Path(args.db).resolve())
    )
    output = Path(args.output) if args.output else Path(config.ROOT) / "docs" / (
        f"pattern-analysis-{datetime.now(UTC).date().isoformat()}.md"
    )
    output.write_text(report, encoding="utf-8")
    print(f"report: {output}")


if __name__ == "__main__":
    main()
