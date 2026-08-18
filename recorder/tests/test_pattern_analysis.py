import numpy as np
import pandas as pd
import pytest
from pattern_analysis import (
    FittedCondition,
    PatternSpec,
    QuantileCondition,
    cluster_uplift_ci,
    daily_stability,
    fit_pattern,
    pattern_mask,
    summarize,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "token_address": ["a", "a", "b", "c", "d", "e"],
            "network_id": ["1"] * 6,
            "split": ["train", "train", "train", "val", "test", "test"],
            "feature": [1.0, 2.0, 3.0, 100.0, 2.0, np.nan],
            "up20": [0, 1, 1, 1, 1, 0],
            "noise": [1, 0, 0, 0, 0, 1],
            "final_win": [0, 1, 1, 1, 1, 0],
            "final20": [0, 0, 1, 1, 0, 0],
            "final_return_48h": [-0.2, 0.1, 0.3, 0.4, 0.1, -0.1],
            "max_gain_24h": [0.05, 0.2, 0.4, 0.5, 0.2, 0.05],
            "max_drawdown_48h": [-0.3, -0.2, -0.1, -0.1, -0.2, -0.3],
            "day": ["2026-01-01"] * 3 + ["2026-01-02"] * 3,
        }
    )


def test_fit_pattern_uses_train_split_only():
    frame = _frame()
    spec = PatternSpec(
        "x", "x", "higher", (QuantileCondition("feature", ">", 0.5),)
    )

    condition = fit_pattern(frame, spec)[0]

    assert condition.threshold == 2.0


def test_pattern_mask_excludes_missing_values():
    frame = _frame().loc[lambda rows: rows["split"] == "test"]
    condition = FittedCondition("feature", ">", 0.5, 1.0)

    assert pattern_mask(frame, (condition,)).tolist() == [True, False]


def test_summarize_reports_rows_tokens_and_outcomes():
    frame = _frame().iloc[:3]
    result = summarize(frame, np.array([False, True, True]))

    assert result["rows"] == 2
    assert result["tokens"] == 2
    assert result["up20_rate"] == 1.0
    assert result["median_final_return"] == pytest.approx(0.2)


def test_cluster_uplift_is_reproducible_and_clusters_by_token():
    frame = pd.DataFrame(
        {
            "token_address": ["a", "a", "b", "b", "c", "c", "d", "d"],
            "network_id": ["1"] * 8,
            "up20": [1, 1, 0, 0, 1, 0, 0, 0],
        }
    )
    selected = np.array([True, True, False, False, True, False, True, False])

    first = cluster_uplift_ci(
        frame, selected, "up20", iterations=500, seed=7
    )
    second = cluster_uplift_ci(
        frame, selected, "up20", iterations=500, seed=7
    )

    assert first == second
    assert first["low"] <= first["estimate"] <= first["high"]


def test_daily_stability_counts_only_days_with_enough_selected_rows():
    frame = _frame()
    condition = FittedCondition("feature", ">", 0.5, 1.5)

    result = daily_stability(
        frame, (condition,), expected_direction="higher", min_rows=1
    )

    assert result == {"consistent": 2, "eligible": 2}
