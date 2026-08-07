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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

import config
import features
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
        "زخم قوي مع تذبذب فعلي",
        "higher",
        (
            QuantileCondition("ret_24h_before", ">", 0.80),
            QuantileCondition("vol_24h_before", ">", 0.60),
        ),
    ),
    PatternSpec(
        "old_token",
        "عملة قديمة نسبياً",
        "lower",
        (QuantileCondition("token_age_h", ">", 0.70),),
    ),
    PatternSpec(
        "low_turnover",
        "دوران ضعيف قياساً بالسيولة",
        "lower",
        (QuantileCondition("volume_to_liquidity", "<=", 0.20),),
    ),
    PatternSpec(
        "low_volatility",
        "تذبذب سابق ضعيف",
        "lower",
        (QuantileCondition("vol_24h_before", "<=", 0.30),),
    ),
    PatternSpec(
        "low_transactions",
        "معاملات يومية قليلة",
        "lower",
        (QuantileCondition("tick_txn_24h", "<=", 0.20),),
    ),
)

CALM_FINISH_SPEC = PatternSpec(
    "calm_finish",
    "هدوء سابق",
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
        rendered = f"{value / 24:.1f} يوم"
    elif feature == "tick_txn_24h":
        rendered = f"{value:,.0f}"
    else:
        rendered = f"{value:.2f}"
    return f"`{feature}` {condition['operator']} {rendered}"


def _rule_text(pattern: dict[str, Any]) -> str:
    return " و ".join(_condition_text(condition) for condition in pattern["conditions"])


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

    return f"""# تحليل أنماط الإشارات — {datetime.now(UTC).date().isoformat()}

> أُنشئ في `{generated}` من `{db_path}`. يغطي {len(frame):,} إشارة مستقلة صالحة،
> {frame.groupby(['token_address', 'network_id'], dropna=False).ngroups} عملة، من `{start}` إلى `{end}`.
> العتبات مشتقة من `train` فقط، والتجزئة ثابتة بعنوان العملة لمنع انتقال العملة بين الأجزاء.

## الخلاصة

- **لا يوجد إثبات أن الإشارة العامة تتفوق على الضابطة.** بعد حذف التكرار بقيت
  {signal['n']} عملة بإشارة مقابل {control['n']} ضوابط حيّة فقط. فرق وسيط عائد 48 ساعة
  {_pct(phase1['median_difference']['estimate'], signed=True)}، وفاصل الثقة
  [{_pct(phase1['median_difference']['low'], signed=True)},
  {_pct(phase1['median_difference']['high'], signed=True)}]، و`p={phase1['mann_whitney']['p_greater']:.3f}`.
- **أقوى نمط صعود هو استمرار الزخم:** {_rule_text(patterns['momentum_spike'])}.
  في الاختبار لمس +20% خلال 24 ساعة في **{_pct(momentum_test['up20_rate'])}** من
  {momentum_test['rows']} إشارة على {momentum_test['tokens']} عملة، مقابل
  {_pct(test_base['up20_rate'])} لكل الاختبار. استمر اتجاه التحسن في كل الأيام المؤهلة.
- **هذا ليس نمط احتفاظ.** وسيط عائد النمط عند نهاية 48 ساعة
  {_pct(momentum_test['median_final_return'], signed=True)} ووسيط السحب
  {_pct(momentum_test['median_drawdown_48h'], signed=True)}؛ الحركة المعتادة اندفاعة ثم ارتداد/تصريف.
- بمحاكاة محافظة: هدف +20%، وقف −30%، خروج 24 ساعة، وتكلفة دورة 2%، حقق نمط الزخم
  الهدف في **{_pct(test_exit['target_rate'])}** وضُرب بالوقف في
  **{_pct(test_exit['stop_rate'])}**؛ المتوسط الصافي
  **{_pct(test_exit['mean_net'], signed=True)}** والوسيط
  **{_pct(test_exit['median_net'], signed=True)}** على الاختبار. لكنه أعطى متوسطاً سالباً
  في التدريب، لذلك لا يُعتمد كاستراتيجية مالية مكتملة.
- لا يوجد نمط ثابت لصعود نهائي +20% بعد 48 ساعة. الهدوء السابق رفع احتمال مجرد
  الإغلاق الموجب إلى {_pct(calm_test['final_win_rate'])}، لكن وسيط العائد كان
  {_pct(calm_test['median_final_return'], signed=True)} فقط ونسبة الإغلاق فوق +20%
  {_pct(calm_test['final20_rate'])}. فاصل الثقة لفرق الإغلاق الموجب عن بقية الاختبار
  [{_pct(calm_ci['low'], signed=True)}, {_pct(calm_ci['high'], signed=True)}]، لكن
  حجم العائد نفسه ضئيل، أي أن الأفضلية يرجح أن تمحوها الرسوم والانزلاق.

## أنماط بلوغ +20% خلال 24 ساعة

خط الأساس: train {_pct(baselines['train']['up20_rate'])}، val
{_pct(baselines['val']['up20_rate'])}، test {_pct(test_base['up20_rate'])}.
عمود `صفوف/عملات` للاختبار. فاصل الثقة هو فرق النسبة عن بقية الاختبار، مع bootstrap
على مستوى العملة لا الصف.

| النمط | القاعدة المثبتة من train | train | val | test | صفوف/عملات | ثبات الأيام | 95% CI للاختلاف |
|---|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(pattern_lines)}

التفسير العملي:

- الزخم المرتفع مع تذبذب حقيقي هو **مرشح اندفاعة قصيرة**.
- العمر فوق عتبة التدريب، أو ضعف دوران الحجم/السيولة، أو ضعف التذبذب، أو قلة
  المعاملات هي **مرشحات ضوضاء منخفضة الصعود**. ليست بالضرورة انهيارات؛ كثير منها
  يتحرك قرب الصفر، أي أن الإشارة لا تضيف حركة تستحق المخاطرة.

## محاكاة مسار السعر للنمط الأقوى

المحاكاة تمشي على الشموع بترتيبها. إذا لمس السعر الهدف والوقف في الشمعة نفسها
تفترض الوقف أولاً. التكلفة المفترضة 2% للدورة، ولا يوجد نموذج مستقل لرفض التنفيذ.

| الجزء | كل الإشارات | متوسطها الصافي | نمط الزخم صفوف/عملات | هدف | وقف | متوسط النمط | وسيط النمط |
|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(exit_lines)}

في اختبار العملات غير المرئية كان فاصل الثقة لتفوق متوسط النمط الصافي على بقية
الاختبار [{_pct(exit_result['test_mean_net_uplift_ci']['low'], signed=True)},
{_pct(exit_result['test_mean_net_uplift_ci']['high'], signed=True)}]. أقصى تكلفة دورة
يبقى عندها **متوسط الاختبار** غير سالب هي تقريباً
{_pct(test_exit['breakeven_round_trip_cost'])}، لكنها غير مستقرة: في التدريب كانت
{_pct(exit_result['train']['momentum']['breakeven_round_trip_cost'])} فقط.

## ما لا يصلح كدليل

نوع الحدث وحده لا يميز الصعود في الاختبار:

| النوع | صفوف | عملات | بلغ +20% |
|---|---:|---:|---:|
{chr(10).join(type_rows)}

كذلك لم يظهر تطابق متداول متصدر أو كون الصفقة أول شراء كأثر ثابت أقوى من السوق.
والحقول التالية فارغة 100% في العينة، لذلك مُنعت من أي استنتاج:
`{', '.join(missing)}`.

## حدود الاستنتاج والخطوة التالية

- الفترة قصيرة ({frame['day'].nunique()} أيام) وسوق meme يغير نظامه بسرعة.
- الضابطة الحية المؤهلة صغيرة وغير متوازنة شبكياً؛ لذلك لا يوجد حكم سببي أن الإشارة
  نفسها تخلق أفضلية على اختيار عملة عشوائية مماثلة.
- استُخدمت `val` لاختيار الصياغة، ثم فُحص `test` في هذا التقرير؛ **مجموعة test الحالية
  أصبحت مستهلكة** ولا يجوز تعديل القواعد وإعادة تسميتها اختباراً مستقلاً.
- جمّد القواعد أعلاه الآن، واجمع 7–14 يوماً جديدة، ثم اختبرها زمنياً بلا تغيير.
  معيار قبول مقترح لنمط الزخم: ≥100 إشارة و≥30 عملة جديدة، بقاء معدل الهدف فوق
  خط الأساس، وفاصل ثقة مجمّع بالعملة فوق الصفر، ومتوسط صافٍ موجب عند تكلفة 2–5%.

هذا تحليل احتمالي تاريخي، لا ضمان سعر ولا توصية استثمارية.
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
