import pytest

from phase1_analysis import (
    _analyze_rows,
    _deduplicate_tokens,
    bootstrap_difference_ci,
    summarize_values,
)


def test_summarize_values_reports_robust_and_tail_metrics():
    result = summarize_values([0.10, -0.20, 0.30, 10.0])

    assert result["n"] == 4
    assert result["median"] == pytest.approx(0.2)
    assert result["mean"] == pytest.approx(2.55)
    assert result["win_rate"] == 0.75


def test_bootstrap_difference_ci_is_reproducible():
    result = bootstrap_difference_ci(
        [0.0, 0.1, 0.2],
        [0.2, 0.3, 0.4],
        statistic="median",
        iterations=500,
        seed=7,
    )

    assert result["estimate"] == pytest.approx(-0.2)
    assert result["low"] <= result["estimate"] <= result["high"]
    assert result == bootstrap_difference_ci(
        [0.0, 0.1, 0.2],
        [0.2, 0.3, 0.4],
        statistic="median",
        iterations=500,
        seed=7,
    )


def test_deduplicate_tokens_keeps_first_window_and_removes_group_overlap():
    rows = [
        {"is_control": 0, "token_address": "a", "network_id": "1", "entry_ts": 1, "key": "a1"},
        {"is_control": 0, "token_address": "a", "network_id": "1", "entry_ts": 2, "key": "a2"},
        {"is_control": 1, "token_address": "b", "network_id": "1", "entry_ts": 3, "key": "b1"},
        {"is_control": 0, "token_address": "b", "network_id": "1", "entry_ts": 4, "key": "b2"},
        {"is_control": 1, "token_address": "c", "network_id": "1", "entry_ts": 5, "key": "c1"},
    ]

    kept, exclusions = _deduplicate_tokens(rows)

    assert [row["key"] for row in kept] == ["a1", "c1"]
    assert exclusions == {
        "duplicate_signal_windows_removed": 1,
        "duplicate_control_windows_removed": 0,
        "overlap_tokens_removed": 1,
    }


def test_analysis_refuses_to_drop_no_bars_outcomes():
    rows = [
        {"is_control": 0, "status": "no_bars"},
        {"is_control": 0, "status": "ok"},
        {"is_control": 1, "status": "ok"},
    ]

    with pytest.raises(RuntimeError, match="no_bars"):
        _analyze_rows(rows)
