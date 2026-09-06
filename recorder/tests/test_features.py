"""Feature extractor tests — **leak guards first**.

The law under test: every feature must be knowable at t=0 or earlier. Each
"leak" test below plants data **after** t0 and verifies that the row does not
change — these tests are the only line of defense against a fake accuracy
that nothing but money would ever expose.
"""
import os

import features
import pytest
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
T0 = 1_785_000_000                     # the decision moment in every test
TOK, NET = "0xtok", "56"
H = 3600


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _iso(epoch: int) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _bars(db, start, n, step=300, c=1.0, grow=0.0, tok=TOK, net=NET, res="5"):
    """`grow` is compound growth per bar (a constant return) — not linear,
    otherwise the relative return decays over time and the rising series
    reads as "flat" at its end."""
    rows = []
    for i in range(n):
        px = c * ((1 + grow) ** i)
        rows.append({
            "token_address": tok, "network_id": net, "resolution": res,
            "ts": start + i * step, "o": px, "h": px * 1.01, "l": px * 0.99,
            "c": px, "v": 10.0,
            "h_suspect": 0, "l_suspect": 0, "c_suspect": 0, "fetched_at": "t",
        })
    db.insert_bars(rows)


def _thesis(db, epoch, tid, user="u1"):
    db.insert_thesis_items([{
        "id": tid, "token_address": TOK, "network_id": NET,
        "created_at": _iso(epoch), "user_handle": user, "user_id": user,
        "num_likes": 5, "num_replies": 0, "equity": 0.0, "trade_id": None,
        "comment": "x", "fetched_at": "t", "raw_json": "{}"}])


def _signal(db, sid, epoch, **kw):
    row = {
        "id": sid, "token_address": TOK, "network_id": NET, "ts": _iso(epoch),
        "recorded_at": _iso(epoch), "signal_type": "large_buy", "ticker": "T",
        "price_usd": 0.001, "fdv": None, "market_cap": 500_000.0,
        "size_usd": 5_000.0, "unique_traders": None, "num_trades": None,
        "minutes": None, "price_change_pct": None, "total_volume": None,
        "are_top_traders": None, "top_trader_ids_json": None,
        "top_trader_match_count": 0, "buyers_best_rank": None,
        "buyer_id": None, "buyer_handle": None, "num_swaps": 2,
        "is_first_buy": 1, "buyer_pnl_pct": None, "avg_cost": None,
        "in_amount": 4_900.0, "in_token_address": None, "out_amount": None,
        "token_amount": None, "realized_pnl_usd": None, "raw_json": "{}",
    }
    row.update(kw)
    db.insert_signal(row)
    return row


# ---------------------------------------------------------------------------
# Leak guards — the heart of this file
# ---------------------------------------------------------------------------
def test_social_ignores_theses_after_t0(db):
    _thesis(db, T0 - 3600, "before")
    before = features.social_features(db, TOK, NET, T0)
    _thesis(db, T0 + 60, "after")          # arrived after the decision moment
    assert features.social_features(db, TOK, NET, T0) == before
    assert before["thesis_counted"] == 1


def test_price_history_ignores_bars_after_t0(db):
    _bars(db, T0 - 86400, 288, c=1.0)      # a full day before t0
    before = features.price_history_features(db, TOK, NET, T0)
    _bars(db, T0 + 300, 20, c=99.0)        # a spike after the decision
    assert features.price_history_features(db, TOK, NET, T0) == before


def test_price_history_ignores_open_bar_at_t0(db):
    """The close of a bar that started before the decision and has not ended
    yet is unknown at t0."""
    _bars(db, T0 - 600, 2, c=1.0)
    db.insert_bars([{
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 60, "o": 1.0, "h": 9.0, "l": 0.5, "c": 8.0,
        "v": 9999.0, "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
        "fetched_at": "t",
    }])
    result = features.price_history_features(db, TOK, NET, T0)
    assert result["bar_vol_1h"] == pytest.approx(20.0)
    assert result["ret_1h_before"] is None


def test_static_snapshot_after_t0_is_invisible(db):
    db._conn.execute(
        """INSERT INTO token_static(
               token_address,network_id,recorded_at,name,symbol,decimals,
               creator_address,raw_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        (TOK, NET, _iso(T0 + 60), "future", "FUT", 9, "creator", "{}"),
    )
    db._conn.commit()
    result = features.static_features(db, TOK, NET, T0)
    assert result["name_len"] is None
    assert result["creator_prior_tokens"] is None


def test_market_tick_after_t0_is_invisible(db):
    db.insert_tick({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 120),
        "source": "trending", "price_usd": 0.001, "liquidity": 50_000.0,
        "holders": 100, "raw_json": "{}"})
    before = features.market_features(db, TOK, NET, T0)
    db.insert_tick({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 + 120),
        "source": "trending", "price_usd": 9.9, "liquidity": 9_000_000.0,
        "holders": 9999, "raw_json": "{}"})
    assert features.market_features(db, TOK, NET, T0) == before
    assert before["liquidity"] == 50_000.0


def test_density_ignores_later_signals_and_self(db):
    _signal(db, "s_prev", T0 - 1800)
    _signal(db, "s_self", T0)
    d = features.density_features(db, TOK, NET, T0, exclude_key="s_self")
    _signal(db, "s_after", T0 + 60)
    assert features.density_features(db, TOK, NET, T0, exclude_key="s_self") == d
    assert d["prior_signals_token"] == 1          # only the prior one, not itself


def test_creator_prior_tokens_respects_t0(db):
    db.upsert_static({
            "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 3600), "name": "A",
        "symbol": "A", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": "0xdev", "launchpad_name": "Pump.fun",
        "migrated": 1, "graduation_percent": 100.0, "twitter": "x", "telegram": None,
        "website": None, "discord": None, "token_created_at": _iso(T0 - 86400),
            "raw_json": "{}",    })
    db.upsert_static({                       # an older coin by the same creator
            "token_address": "0xold", "network_id": NET, "recorded_at": _iso(T0 - 3600), "name": "B",
        "symbol": "B", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": "0xdev", "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": _iso(T0 - 200_000), "raw_json": "{}"})
    s = features.static_features(db, TOK, NET, T0)
    assert s["creator_prior_tokens"] == 1
    db.upsert_static({                       # a **later** coin by the same creator
        "token_address": "0xnew", "network_id": NET,
        "recorded_at": _iso(T0 - 3600), "name": "C",
        "symbol": "C", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": "0xdev", "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": _iso(T0 + 100_000), "raw_json": "{}"})
    assert features.static_features(db, TOK, NET, T0)["creator_prior_tokens"] == 1


# ---------------------------------------------------------------------------
# Correctness of the computation
# ---------------------------------------------------------------------------
def test_event_features_derive_ratios_and_logs():
    f = features.event_features(
        {"signal_type": "multi_user_buy", "size_usd": 10_000.0,
         "market_cap": 1_000_000.0, "total_volume": 60_000.0,
         "unique_traders": 12, "buyers_best_rank": 7}, T0)
    assert f["size_to_mcap"] == pytest.approx(0.01)
    # (fv14) volume_per_trader was dropped from the features along with its
    # dead family — the remaining computation is tested via its live columns only.
    assert f["rank_le_10"] == 1 and f["rank_le_50"] == 1
    assert f["log_market_cap"] > f["log_size_usd"]


def test_event_features_missing_stay_none_not_zero():
    f = features.event_features({"signal_type": "large_buy"}, T0)
    assert f["size_to_mcap"] is None
    assert f["rank_le_10"] is None          # no rank ⇒ no flag (not zero)


# --- the per-period rank family (v7) ---
def test_period_rank_features_pass_through_and_aggregate():
    """Detailed ranks pass through, and the aggregate takes the best across
    all periods."""
    f = features.event_features({
        "signal_type": "large_buy",
        "buyers_best_rank": 40,
        "buyers_best_rank_24h": 3,
        "top_trader_match_count_24h": 1,
        "buyers_best_rank_7d": 12,
        "top_trader_match_count_7d": 2,
        "top_trader_periods_matched": 3,
    }, T0)
    assert f["buyers_best_rank_24h"] == 3
    assert f["top_trader_match_count_7d"] == 2
    assert f["best_rank_any_period"] == 3     # the best of the four
    assert f["top_trader_periods_matched"] == 3
    assert f["top_trader_any_period"] == 1


def test_period_rank_features_absent_stay_none():
    f = features.event_features({"signal_type": "large_buy"}, T0)
    for c in (
        "buyers_best_rank_24h", "top_trader_match_count_24h",
        "buyers_best_rank_7d", "buyers_best_rank_30d",
        "top_trader_periods_matched", "top_trader_any_period",
        "best_rank_any_period",
    ):
        assert f[c] is None, c


def test_any_period_zero_is_measured_not_missing():
    """Periods were measured and nobody matched ⇒ 0, not None, while the best
    rank stays absent."""
    f = features.event_features({
        "signal_type": "large_buy", "top_trader_periods_matched": 0,
    }, T0)
    assert f["top_trader_any_period"] == 0
    assert f["best_rank_any_period"] is None


def test_best_rank_any_period_ignores_missing_periods():
    """One measured period is enough — min over a list containing None neither
    raises nor fabricates."""
    f = features.event_features({
        "signal_type": "large_buy", "buyers_best_rank_30d": 9,
    }, T0)
    assert f["best_rank_any_period"] == 9


def test_price_history_computes_returns_and_flatness(db):
    _bars(db, T0 - 86400, 288, c=1.0, grow=0.0)     # perfectly flat
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["ret_24h_before"] == pytest.approx(0.0)
    assert f["flat_ratio_24h"] == pytest.approx(1.0)   # every bar is flat
    assert f["bars_count_24h"] > 200
    assert f["bars_history_h"] == pytest.approx(24.0, abs=0.1)


def test_price_history_detects_pre_signal_pump(db):
    _bars(db, T0 - 86400, 288, c=1.0, grow=0.01)      # an accelerating climb before the signal
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["ret_24h_before"] > 1.0
    assert f["flat_ratio_24h"] < 0.5
    assert f["dist_from_ath"] is not None and f["dist_from_ath"] <= 0


def test_price_history_skips_suspect_closes(db):
    _bars(db, T0 - 3600, 12, c=1.0)
    db.insert_bars([{                                  # a flagged suspect close
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 300, "o": 1.0, "h": 9e9, "l": 1.0, "c": 12_052.5,
        "h_suspect": 1, "l_suspect": 0, "c_suspect": 1, "fetched_at": "t",
    }])
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["ret_1h_before"] == pytest.approx(0.0)     # the corrupt close was not used


def test_social_accel_and_history(db):
    _thesis(db, T0 - 90_000, "old", user="a")          # a day and a half before
    _thesis(db, T0 - 7200, "mid", user="b")
    _thesis(db, T0 - 600, "new", user="c")
    f = features.social_features(db, TOK, NET, T0)
    assert f["thesis_counted"] == 3
    assert f["thesis_authors_before"] == 3
    assert f["thesis_1h"] == 1 and f["thesis_24h"] == 2
    assert f["thesis_accel"] == pytest.approx(0.5)
    assert f["hours_since_last_thesis"] == pytest.approx(600 / 3600)
    assert f["thesis_history_days"] > 1.0


def test_social_features_do_not_cross_networks(db):
    db.insert_thesis_items([{
        "id": "other-network", "token_address": TOK, "network_id": "1",
        "created_at": _iso(T0 - 60), "user_id": "u", "raw_json": "{}",
    }])
    assert features.social_features(db, TOK, NET, T0)["thesis_counted"] == 0


def test_macro_features_read_hourly_reference(db):
    _label, addr, net = features.config.MACRO_BARS[0]
    _bars(db, T0 - 86400 * 2, 48, step=3600, c=100.0, grow=0.01,
          tok=addr, net=net, res=features.config.MACRO_BARS_RESOLUTION)
    f = features.macro_features(db, T0)
    assert f["sol_ret_24h"] is not None


def test_macro_features_ignore_open_hour(db):
    label, addr, net = features.config.MACRO_BARS[0]
    assert label == "SOL"
    _bars(db, T0 - 25 * H, 25, step=H, c=100.0, grow=0.0,
          tok=addr, net=net, res=features.config.MACRO_BARS_RESOLUTION)
    db.insert_bars([{
        "token_address": addr, "network_id": net,
        "resolution": features.config.MACRO_BARS_RESOLUTION,
        "ts": T0 - 60, "o": 100.0, "h": 1000.0, "l": 10.0, "c": 900.0,
        "v": 1.0, "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
        "fetched_at": "t",
    }])
    assert features.macro_features(db, T0)["sol_ret_24h"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The full row
# ---------------------------------------------------------------------------
def _outcome(db, key="s1", kind="signal"):
    db.insert_outcome({
        "kind": kind, "key": key, "token_address": TOK, "network_id": NET,
        "signal_type": "large_buy", "is_control": 0, "is_independent": 1,
        "entry_ts": T0, "entry_px": 1.0, "entry_lag_s": 60,
        "max_gain_1h": 0.1, "max_gain_4h": 0.2, "max_gain_24h": 0.5,
        "max_gain_48h": 0.5, "max_drawdown_48h": -0.3, "final_return_48h": 0.4,
        "time_to_peak_h": 3.0, "candles_48h": 500, "suspect_bars": 0,
        "last_bar_lag_h": 0.1, "bars_truncated": 0, "is_rug": 0,
        "split": "train", "status": "ok", "labeled_at": "t",
    })
    return dict(db._conn.execute(
        "SELECT * FROM outcomes WHERE kind=? AND key=?", (kind, key)).fetchone())


def test_build_training_row_has_every_declared_column(db):
    _signal(db, "s1", T0)
    _bars(db, T0 - 3600, 12)
    row = features.build_training_row(db, _outcome(db))
    assert row is not None
    missing = [c for c in features.ROW_COLUMNS if c not in row]
    assert missing == []
    assert row["kind"] == "signal" and row["entry_ts"] == T0
    assert row["final_return_48h"] == 0.4          # the label is attached
    assert row["market_cap"] == 500_000.0          # the feature comes from the event


def test_build_training_row_returns_none_without_source_event(db):
    assert features.build_training_row(db, _outcome(db, key="ghost")) is None


def test_build_training_row_marks_asset_class(db):
    _signal(db, "s1", T0, ticker="MEME", price_usd=0.001, market_cap=500_000.0)
    db._conn.execute(
        "INSERT INTO token_class(token_address, network_id, asset_class, classified_at) "
        "VALUES(?,?,?,?)", (TOK, NET, "meme", "t"))
    db._conn.commit()
    row = features.build_training_row(db, _outcome(db))
    assert row["asset_class"] == "meme"


def test_asset_class_uses_only_event_time_observations(db):
    _signal(db, "s1", T0, price_usd=0.001, market_cap=500_000.0, ticker="MEME")
    db._conn.execute(
        "INSERT INTO token_class(token_address,network_id,asset_class,classified_at) "
        "VALUES(?,?,?,?)",
        (TOK, NET, "priced", _iso(T0 + 86400)),
    )
    db._conn.commit()
    row = features.build_training_row(db, _outcome(db))
    assert row["asset_class"] == "meme"


def test_is_live_flag_splits_on_live_start(db):
    """The flag splits the two eras exactly at LIVE_START_TS: before it retro,
    at/after it live. This filter is what excludes retro data from training —
    breaking it re-leaks the era."""
    cut = features.config.LIVE_START_TS
    # retro: one second before the cut
    _signal(db, "retro", cut - 1)
    _bars(db, cut - 3600, 12)
    r_retro = features.build_training_row(
        db, _outcome_at(db, "retro", cut - 1))
    assert r_retro["is_live"] == 0
    # live: exactly at the cut
    _signal(db, "live", cut)
    r_live = features.build_training_row(db, _outcome_at(db, "live", cut))
    assert r_live["is_live"] == 1


def test_activity_rows_are_never_marked_live(db):
    db.insert_activity_events([{
        "id": "retro", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0), "recorded_at": _iso(T0 + 3600),
        "ticker": "MEME", "price_usd": 0.001, "market_cap": 500_000.0,
        "raw_json": "{}",
    }])
    outcome = _outcome(db, key="retro", kind="activity")
    assert features.build_training_row(db, outcome)["is_live"] == 0


def _outcome_at(db, key, entry_ts):
    o = _outcome(db, key=key)
    db._conn.execute(
        "UPDATE outcomes SET entry_ts=? WHERE kind='signal' AND key=?",
        (entry_ts, key))
    db._conn.commit()
    o["entry_ts"] = entry_ts
    return o


def test_label_columns_are_never_features():
    """A structural guard: no label column sneaks into the feature list."""
    assert not set(features.LABEL_COLUMNS) & set(features.FEATURE_COLUMNS)
    for banned in ("final_return", "max_gain", "max_drawdown", "is_rug", "time_to_peak"):
        assert not any(banned in c for c in features.FEATURE_COLUMNS)


def test_num_likes_is_not_a_feature():
    """Likes are banned: their value is fetch-time, not write-time (README §9)."""
    assert not any("like" in c.lower() for c in features.FEATURE_COLUMNS)


# ---------------------------------------------------------------------------
# Audit fixes (2026-07-30) — each test prevents the return of a measured defect
# ---------------------------------------------------------------------------
def test_ath_ignores_suspect_high(db):
    """39 bars with a sound close and a corrupt high used to give
    dist_from_ath = −0.99999997 on 49 rows (a high of 96,311 instead of
    0.0143)."""
    _bars(db, T0 - 3600, 12, c=1.0)
    db.insert_bars([{
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 600, "o": 1.0, "h": 96_311.0, "l": 0.9, "c": 1.0,
        "h_suspect": 1, "l_suspect": 0, "c_suspect": 0, "fetched_at": "t",
    }])
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["dist_from_ath"] > -0.5          # not −0.99999997


def test_measured_zero_size_is_kept_not_nulled(db):
    """1,640 events with a genuinely 0.0 size (a full exit): `a or b` used to
    turn it into None."""
    f = features.event_features({"signal_type": "large_sell", "size_usd": 0.0}, T0)
    assert f["size_usd"] == 0.0
    # and the fallback to usd_amount happens only on true absence
    f2 = features.event_features({"event_type": "swap_buy", "usd_amount": 26.7}, T0)
    assert f2["size_usd"] == 26.7


def test_density_counts_both_sources(db):
    """Counting from signal_events alone made 88% of retro rows structural
    zeros versus 4% of live ones — the column indicated row type, not coin
    activity."""
    db.insert_activity_events([{
        "id": "a1", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0 - 1800), "recorded_at": "t", "raw_json": "{}",
    }])
    d = features.density_features(db, TOK, NET, T0, exclude_key=None)
    assert d["prior_signals_token"] == 1              # the retro event is counted
    assert d["global_signals_1h"] == 1
    assert d["minutes_since_prior_signal"] == pytest.approx(30.0)


def test_density_deduplicates_same_event_across_sources(db):
    _signal(db, "same", T0 - 1800)
    db.insert_activity_events([{
        "id": "same", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0 - 1800), "recorded_at": "t",
        "raw_json": "{}",
    }])
    result = features.density_features(db, TOK, NET, T0, exclude_key=None)
    assert result["prior_signals_token"] == 1
    assert result["global_signals_1h"] == 1


def test_density_does_not_cross_networks(db):
    db.insert_activity_events([{
        "id": "other", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": "1", "ts": _iso(T0 - 1800), "recorded_at": "t",
        "raw_json": "{}",
    }])
    assert features.density_features(
        db, TOK, NET, T0, exclude_key=None
    )["prior_signals_token"] == 0


def test_density_still_excludes_self(db):
    db.insert_activity_events([{
        "id": "self", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0), "recorded_at": "t", "raw_json": "{}",
    }])
    assert features.density_features(
        db, TOK, NET, T0, exclude_key="self")["prior_signals_token"] == 0


def _social(db, epoch, total, authors, holders, replies=0):
    db.insert_social({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(epoch),
        "thesis_count": min(total, 100), "thesis_total": total,
        "thesis_sampled": min(total, 100), "has_next_page": 1 if total > 100 else 0,
        "thesis_likes": 0, "thesis_replies": replies, "thesis_authors": authors,
        "holder_authors": holders, "newest_thesis_at": _iso(epoch), "raw_json": "{}",
    })


def test_social_snapshot_gives_true_total_and_holder_ratio(db):
    """`token_thesis` is a sample (cap ~400) while `thesis_total` is the truth
    (29,595)."""
    _social(db, T0 - 600, total=29_595, authors=80, holders=20, replies=7)
    f = features.social_features(db, TOK, NET, T0)
    assert f["social_thesis_total"] == 29_595
    assert f["social_holder_authors"] == 20
    assert f["social_holder_ratio"] == pytest.approx(0.25)
    assert f["social_replies"] == 7
    assert f["social_snapshot_age_min"] == pytest.approx(10.0)


def test_social_momentum_delta_from_two_snapshots(db):
    _social(db, T0 - 7200, total=100, authors=10, holders=3)
    _social(db, T0 - 300, total=160, authors=18, holders=6)
    f = features.social_features(db, TOK, NET, T0)
    assert f["social_total_delta_1h"] == 60
    assert f["social_total_growth_1h"] == pytest.approx(0.6)
    assert f["social_authors_delta_1h"] == 8


def test_social_snapshot_after_t0_is_invisible(db):
    _social(db, T0 - 300, total=50, authors=5, holders=2)
    before = features.social_features(db, TOK, NET, T0)
    _social(db, T0 + 300, total=9_999, authors=500, holders=400)
    assert features.social_features(db, TOK, NET, T0) == before


def test_thesis_cap_flag_marks_saturated_count(db):
    for i in range(_cap := features._THESIS_PAGE_CAP):
        _thesis(db, T0 - 1000 - i, f"t{i}", user=f"u{i % 7}")
    f = features.social_features(db, TOK, NET, T0)
    assert f["thesis_counted"] >= _cap
    assert f["thesis_counted_capped"] == 1


def test_market_dense_windows_and_ratios(db):
    db.insert_tick({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 60),
        "source": "trending", "price_usd": 0.001, "liquidity": 50_000.0,
        "market_cap": 500_000.0, "holders": 100, "change_1h": 0.12,
        "change_4h": 0.4, "change_24h": 1.2, "volume_1h": 20_000.0,
        "volume_4h": 60_000.0, "volume_24h": 150_000.0, "txn_count_1h": 300,
        "txn_count_24h": 2_000, "circulating_supply": 600_000_000.0,
        "total_supply": 1_000_000_000.0, "raw_json": "{}"})
    f = features.market_features(db, TOK, NET, T0)
    assert f["tick_change_1h"] == 0.12 and f["tick_volume_4h"] == 60_000.0
    assert f["tick_txn_1h"] == 300
    assert f["volume_to_liquidity"] == pytest.approx(3.0)
    assert f["liquidity_to_mcap"] == pytest.approx(0.1)
    assert f["float_ratio"] == pytest.approx(0.6)


# --- merging market snapshots across sources (v8) ---------------------------
# The two sources are not equivalent, measured on 200k rows: `verified` carries
# price and liquidity 100% of the time and **zero percent** of the counters,
# while `trending` carries them all. Taking only "the newest row" inherited
# poverty from whichever source happened to write last — for 50% of coins.
# These tests guard the fix.
def _tick(db, epoch, source, **cols):
    db.insert_tick({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(epoch),
        "source": source, "raw_json": "{}", **cols,
    })


_RICH = {"buy_count_24h": 400, "sell_count_24h": 100, "unique_buys_24h": 250,
         "unique_sells_24h": 80, "holders": 900, "top10_holders_pct": 42.0}


def test_market_merge_recovers_counters_from_older_rich_row(db):
    """A newer, poor `verified` row on top of an older, rich `trending` row.

    Before the fix every counter was None because the newer row does not carry
    them — while they sit in the table.
    """
    _tick(db, T0 - 3600, "trending", liquidity=10_000.0, volume_24h=1_000.0, **_RICH)
    _tick(db, T0 - 60, "verified", liquidity=50_000.0, volume_24h=150_000.0)

    f = features.market_features(db, TOK, NET, T0)

    assert f["buy_count_24h"] == 400            # from the older row
    assert f["unique_buys_24h"] == 250
    assert f["holders"] == 900
    assert f["top10_holders_pct"] == 42.0
    assert f["buy_sell_ratio_24h"] == pytest.approx(4.0)   # derived **after** the merge
    # price and liquidity come from the newer row, not the older one:
    assert f["liquidity"] == 50_000.0
    assert f["volume_to_liquidity"] == pytest.approx(3.0)


def test_market_merge_stamps_two_ages_not_one(db):
    """Honest freshness: a counter an hour old is not read as one minute old."""
    _tick(db, T0 - 3600, "trending", liquidity=10_000.0, **_RICH)
    _tick(db, T0 - 60, "verified", liquidity=50_000.0)

    f = features.market_features(db, TOK, NET, T0)

    assert f["tick_age_min"] == pytest.approx(1.0)        # newest row
    assert f["tick_rich_age_min"] == pytest.approx(60.0)  # the counters' source


def test_market_merge_rich_age_follows_newest_rich_row(db):
    """When the newest row is itself rich, the two stamps match — no fake
    staleness."""
    _tick(db, T0 - 3600, "trending", liquidity=10_000.0, **_RICH)
    _tick(db, T0 - 120, "trending", liquidity=50_000.0, **_RICH)

    f = features.market_features(db, TOK, NET, T0)

    assert f["tick_age_min"] == pytest.approx(2.0)
    assert f["tick_rich_age_min"] == pytest.approx(2.0)


def test_market_merge_without_any_rich_row_leaves_age_null(db):
    """No counter within the range ⇒ `tick_rich_age_min` = None, not zero
    (FR-007)."""
    _tick(db, T0 - 60, "verified", liquidity=50_000.0, volume_24h=150_000.0)

    f = features.market_features(db, TOK, NET, T0)

    assert f["tick_age_min"] == pytest.approx(1.0)
    assert f["tick_rich_age_min"] is None
    assert f["buy_count_24h"] is None
    assert f["buy_sell_ratio_24h"] is None      # not derived from absence


def test_market_merge_keeps_measured_zero(db):
    """A measured zero ≠ absent: `x is None`, not `x or y` — otherwise the
    older row swallowed the zero."""
    _tick(db, T0 - 3600, "trending", liquidity=10_000.0,
          buy_count_24h=400, sell_count_24h=100, volume_24h=9_999.0)
    _tick(db, T0 - 60, "trending", liquidity=50_000.0,
          buy_count_24h=0, sell_count_24h=0, volume_24h=0.0)

    f = features.market_features(db, TOK, NET, T0)

    assert f["buy_count_24h"] == 0              # not 400
    assert f["volume_24h"] == 0.0
    assert f["buy_sell_ratio_24h"] is None      # division by zero ⇒ None, not inf
    assert f["tick_rich_age_min"] == pytest.approx(1.0)


def test_market_merge_fills_each_column_from_its_own_newest_row(db):
    """The merge is column by column, not whole-row: each column comes from
    the newest row that carries it."""
    _tick(db, T0 - 1800, "trending", holders=700, total_supply=1_000.0)
    _tick(db, T0 - 600, "verified", market_cap=500_000.0)
    _tick(db, T0 - 60, "filter", liquidity=50_000.0)

    f = features.market_features(db, TOK, NET, T0)

    assert f["liquidity"] == 50_000.0
    assert f["holders"] == 700
    assert f["liquidity_to_mcap"] == pytest.approx(0.1)   # from two different rows


def test_market_merge_ignores_rows_after_t0(db):
    """The point-in-time law precedes the merge: a rich row after t0 is not
    summoned to patch poverty."""
    _tick(db, T0 - 60, "verified", liquidity=50_000.0)
    before = features.market_features(db, TOK, NET, T0)
    _tick(db, T0 + 30, "trending", liquidity=99.0, **_RICH)

    assert features.market_features(db, TOK, NET, T0) == before
    assert before["buy_count_24h"] is None
    assert before["tick_rich_age_min"] is None


def test_market_merge_lookback_is_bounded(db):
    """The lookback is bounded on purpose: a counter older than the search
    window is not resurrected.

    12 rows ≈ three cycles across their four sources. Beyond that it is old
    enough to lie.
    """
    _tick(db, T0 - 7200, "trending", **_RICH)
    for i in range(features._TICK_MERGE_LOOKBACK):
        _tick(db, T0 - 60 * (i + 1), "verified", liquidity=50_000.0)

    f = features.market_features(db, TOK, NET, T0)

    assert f["liquidity"] == 50_000.0
    assert f["buy_count_24h"] is None
    assert f["tick_rich_age_min"] is None


def test_bar_volume_surge_before_signal(db):
    _bars(db, T0 - 86400, 288, c=1.0)                 # volume 10 per bar
    db.insert_bars([{
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 300, "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05, "v": 5_000.0,
        "h_suspect": 0, "l_suspect": 0, "c_suspect": 0, "fetched_at": "t",
    }])
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["bar_vol_1h"] >= 5_000.0
    assert f["bar_vol_24h"] > f["bar_vol_1h"]
    assert f["vol_surge_1h"] > 0.5                     # the last hour dominates
    assert 0.0 <= f["up_candle_ratio_24h"] <= 1.0


def test_daily_ath_is_used_only_after_history_is_complete(db):
    _bars(db, T0 - 3600, 12, c=1.5)
    db.insert_bars([
        {
            "token_address": TOK, "network_id": NET, "resolution": "1D",
            "ts": T0 - 3 * 86400, "o": 4.0, "h": 10.0, "l": 3.0,
            "c": 5.0, "v": 1.0, "h_suspect": 0, "l_suspect": 0,
            "c_suspect": 0, "fetched_at": "t",
        },
        {   # the day has not closed by t0; no ATH may leak from it
            "token_address": TOK, "network_id": NET, "resolution": "1D",
            "ts": T0 - 12 * 3600, "o": 5.0, "h": 99.0, "l": 4.0,
            "c": 90.0, "v": 1.0, "h_suspect": 0, "l_suspect": 0,
            "c_suspect": 0, "fetched_at": "t",
        },
    ])
    db._conn.execute(
        """INSERT INTO historical_bars_state(
               token_address,network_id,resolution,cursor_to,oldest_ts,last_status,
               candles,calls,attempts,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (TOK, NET, "1D", T0, T0 - 3 * 86400, "partial", 2, 1, 1, "t"),
    )
    partial = features.price_history_features(db, TOK, NET, T0)
    assert partial["ath_history_complete"] == 0
    assert partial["dist_from_ath"] > -0.5

    db._conn.execute(
        "UPDATE historical_bars_state SET last_status='ok' "
        "WHERE token_address=? AND network_id=? AND resolution='1D'",
        (TOK, NET),
    )
    complete = features.price_history_features(db, TOK, NET, T0)
    assert complete["ath_history_complete"] == 1
    assert complete["ath_history_days"] == pytest.approx(3.0)
    assert complete["dist_from_ath"] == pytest.approx(1.5 / 10.0 - 1.0)


def test_buyer_and_text_features(db):
    f = features.event_features({
        "signal_type": "large_buy", "price_usd": 0.002, "avg_cost": 0.001,
        "token_amount": 5_000.0, "out_amount": 4_900.0, "ticker": "PEPE2é",
        "top_trader_ids_json": '["a","b","c"]', "top_trader_match_count": 1,
    }, T0)
    assert f["price_to_avg_cost"] == pytest.approx(2.0)   # buys above his average
    assert f["token_amount"] == 5_000.0
    assert f["top_traders_listed"] == 3
    # (fv14) top_trader_match_ratio was dropped along with its dead family; the
    # absolute match count remains a live feature and is what is tested here.
    assert f["top_trader_match_count"] == 1
    assert f["ticker_len"] == 6
    assert f["ticker_has_digit"] == 1
    assert f["ticker_non_ascii"] == 1


def test_static_name_and_decimals(db):
    db.upsert_static({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 60),
        "name": "Café Coin", "symbol": "CAFE", "decimals": 9, "mintable": None,
        "freezable": None, "is_scam": None, "creator_address": None,
        "launchpad_name": None, "migrated": None, "graduation_percent": None,
        "twitter": None, "telegram": None, "website": None, "discord": None,
        "token_created_at": T0 - 3600, "raw_json": "{}"})
    f = features.static_features(db, TOK, NET, T0)
    assert f["decimals"] == 9
    assert f["name_len"] == 9
    assert f["name_non_ascii"] == 1


def test_no_feature_column_lost_in_refactor():
    """Every declared feature is actually produced — prevents a declared
    column with no computation (eternal NULL)."""
    produced = set()
    produced |= set(features.event_features({}, T0))
    assert set(features.FEATURE_COLUMNS) >= produced


def test_model_training_view_enforces_safe_cohort(db):
    cols = features.ROW_COLUMNS
    base = {c: None for c in cols}
    base.update({
        "key": "safe", "kind": "signal", "token_address": TOK,
        "network_id": NET, "entry_ts": T0, "asset_class": "meme",
        "split": "train", "is_independent": 1, "is_live": 1,
        "status": "ok", "feature_version": features.FEATURE_VERSION,
        "built_at": _iso(T0 + 49 * H),
    })
    excluded = dict(base, key="activity", kind="activity", is_live=0)
    db._conn.executemany(
        f"INSERT INTO training_rows({', '.join(cols)}) "
        f"VALUES({', '.join('?' for _ in cols)})",
        [tuple(row[c] for c in cols) for row in (base, excluded)],
    )
    rows = db._conn.execute("SELECT key FROM model_training_rows").fetchall()
    assert [row["key"] for row in rows] == ["safe"]


def test_model_training_view_deduplicates_semantic_feed_events(db):
    _signal(db, "a", T0)
    event = _signal(db, "b", T0)
    db._conn.execute(
        "INSERT INTO signal_events(id,token_address,network_id,ts,recorded_at,"
        "signal_type,raw_json) VALUES(?,?,?,?,?,?,?)",
        ("b", TOK, NET, event["ts"], event["recorded_at"],
         event["signal_type"], "{}"),
    )
    db._conn.commit()
    cols = features.ROW_COLUMNS
    rows = []
    for key in ("a", "b"):
        row = {c: None for c in cols}
        row.update({
            "kind": "signal", "key": key, "token_address": TOK,
            "network_id": NET, "entry_ts": T0, "asset_class": "meme",
            "split": "train", "is_independent": 1, "is_live": 1,
            "status": "ok", "feature_version": features.FEATURE_VERSION,
            "built_at": _iso(T0 + 49 * H),
        })
        rows.append(row)
    db._conn.executemany(
        f"INSERT INTO training_rows({', '.join(cols)}) "
        f"VALUES({', '.join('?' for _ in cols)})",
        [tuple(row[c] for c in cols) for row in rows],
    )
    kept = db._conn.execute("SELECT key FROM model_training_rows").fetchall()
    assert [row["key"] for row in kept] == ["a"]


def test_incremental_builder_rebuild_does_not_repeat_current_version(db):
    from build_training_rows import pending_outcomes

    _signal(db, "s1", T0, ticker="MEME", price_usd=0.001, market_cap=500_000.0)
    _outcome(db, key="s1")
    first = pending_outcomes(db, rebuild=False, limit=10)
    assert [row["key"] for row in first] == ["s1"]
    db._conn.execute(
        "INSERT INTO training_rows(kind,key,token_address,network_id,entry_ts,"
        "feature_version,built_at) VALUES(?,?,?,?,?,?,?)",
        ("signal", "s1", TOK, NET, T0, features.FEATURE_VERSION, _iso(T0)),
    )
    db._conn.commit()
    assert pending_outcomes(db, rebuild=False, limit=10) == []


def test_dry_rebuild_never_deletes_existing_training_rows(db):
    import build_training_rows

    _signal(db, "s1", T0)
    _outcome(db, key="s1")
    built = build_training_rows.build(db, False, 10, False)
    assert built["built"] == 1

    build_training_rows.build(db, True, 10, True)

    assert db._conn.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0] == 1


def test_incremental_builder_blocks_evm_rows_until_rebuild_is_ready(db, monkeypatch):
    from build_training_rows import pending_outcomes

    _signal(db, "s1", T0)
    _outcome(db, key="s1")
    monkeypatch.setattr(features.config, "EVM_NETWORKS", (NET,))
    monkeypatch.setattr(features.config, "EVM_REPLAY_NETWORKS", (NET,))
    db.set_meta("evm_ledger_rebuild_required", "1")
    db.set_meta("evm_training_rebuild_started", "0")

    assert pending_outcomes(db, rebuild=False, limit=10) == []

    db.set_meta("evm_training_rebuild_started", "1")
    assert [row["key"] for row in pending_outcomes(db, False, 10)] == ["s1"]


def test_incremental_builder_rejects_rows_computed_before_finalization(
    db, monkeypatch,
):
    import build_training_rows
    from db import StaleEVMState

    _signal(db, "s1", T0)
    _outcome(db, key="s1")
    db.set_meta("evm_ledger_rebuild_required", "1")
    db.set_meta("evm_training_rebuild_started", "1")
    db_path = db._conn.execute("PRAGMA database_list").fetchone()["file"]
    other = RecorderDB(db_path, SCHEMA)
    original = features.build_training_row

    def _finalize_during_compute(current_db, outcome):
        row = original(current_db, outcome)
        other.set_meta("evm_training_rebuild_started", "0")
        other.set_meta("evm_ledger_rebuild_required", "0")
        return row

    monkeypatch.setattr(features, "build_training_row", _finalize_during_compute)
    try:
        with pytest.raises(StaleEVMState, match="training rebuild"):
            build_training_rows.build(db, False, 10, False)
    finally:
        other.close()

    assert db._conn.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0] == 0


def test_incremental_builder_can_limit_work_to_live_independent_signals(db):
    from build_training_rows import pending_outcomes

    cut = features.config.LIVE_START_TS
    for key, ts in (("old", cut - 1), ("live", cut), ("duplicate", cut + 1)):
        _signal(db, key, ts)
        _outcome_at(db, key, ts)
    db._conn.execute(
        "UPDATE outcomes SET is_independent=0 WHERE kind='signal' AND key='duplicate'"
    )
    _outcome_at(db, "watch", cut + 2)
    db._conn.execute("UPDATE outcomes SET kind='watch' WHERE key='watch'")
    db._conn.commit()

    rows = pending_outcomes(
        db, rebuild=False, limit=10, model_candidates_only=True
    )
    assert [row["key"] for row in rows] == ["live"]


# ---------------------------------------------------------------------------
# Inconsistent timestamp formats from fomo — exposed by the coverage report
# (token_age_h empty 100% of the time)
# ---------------------------------------------------------------------------
def test_epoch_of_accepts_iso_and_numeric_epoch():
    assert features.epoch_of("2026-07-30T10:00:00Z") == 1785405600
    assert features.epoch_of("2026-07-30T10:00:00+00:00") == 1785405600
    assert features.epoch_of(1784983617) == 1784983617       # numeric epoch
    assert features.epoch_of("1784983617") == 1784983617     # numeric text
    assert features.epoch_of(1784983617000) == 1784983617    # milliseconds
    assert features.epoch_of(None) is None
    assert features.epoch_of("") is None
    assert features.epoch_of("not-a-date") is None


def test_token_age_computed_from_numeric_created_at(db):
    """fomo stores `token_created_at` as a numeric epoch — it used to read as
    None, so the age was lost."""
    db.upsert_static({
        "token_address": TOK, "network_id": NET,
        "recorded_at": _iso(T0 - 60), "name": "A",
        "symbol": "A", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": None, "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": T0 - 7200,          # numeric, not ISO
        "raw_json": "{}"})
    f = features.static_features(db, TOK, NET, T0)
    assert f["token_age_h"] == pytest.approx(2.0)


def test_negative_token_age_is_dropped_not_learned(db):
    """A fomo stamp may mark listing rather than creation, so it precedes t0
    (measured: −177 hours). A negative age is impossible ⇒ None, not a number
    the model would learn from."""
    db.upsert_static({
        "token_address": TOK, "network_id": NET,
        "recorded_at": _iso(T0 - 60), "name": "A",
        "symbol": "A", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": None, "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": T0 + 86400,          # "created" after the signal
        "raw_json": "{}"})
    assert features.static_features(db, TOK, NET, T0)["token_age_h"] is None


def test_creator_prior_tokens_with_numeric_timestamps(db):
    for addr, created in ((TOK, T0 - 3600), ("0xold", T0 - 100_000), ("0xnew", T0 + 5)):
        db.upsert_static({
            "token_address": addr, "network_id": NET,
            "recorded_at": _iso(T0 - 60), "name": "x",
            "symbol": "x", "decimals": 18, "mintable": None, "freezable": None,
            "is_scam": None, "creator_address": "0xdev", "launchpad_name": None,
            "migrated": None, "graduation_percent": None, "twitter": None,
            "telegram": None, "website": None, "discord": None,
            "token_created_at": created, "raw_json": "{}"})
    assert features.static_features(db, TOK, NET, T0)["creator_prior_tokens"] == 1


# ---------------------------------------------------------------------------
# Family H2 — ownership: chain concentration + platform crowd positioning
# ---------------------------------------------------------------------------
def _holders(db, epoch, source, tok=TOK, net=NET, **kw):
    """A holders row. `source` is part of the primary key, so one source
    cannot erase the other."""
    row = {
        "token_address": tok, "network_id": net, "recorded_at": _iso(epoch),
        "watch_first_seen_at": _iso(epoch), "entry_signal_id": None,
        "is_control": 0, "source": source,
        "top10_pct": None, "holder_count": None, "platform_holders": None,
        "platform_holders_listed": None, "platform_value_usd": None,
        "platform_underwater": None, "platform_median_hold_seconds": None,
        "platform_dev_holding": None, "top_holders_json": None, "raw_json": "{}",
    }
    row.update(kw)
    db.insert_holders(row)
    return row


def test_holders_ignores_measurements_after_t0(db):
    """Leak guard: a concentration measurement after the decision is unknown
    at it."""
    _holders(db, T0 - 600, "token_details", top10_pct=22.0, holder_count=900)
    before = features.holders_features(db, TOK, NET, T0)
    _holders(db, T0 + 600, "token_details", top10_pct=99.0, holder_count=5)
    assert features.holders_features(db, TOK, NET, T0) == before
    assert before["chain_top10_pct"] == 22.0


def test_holders_uses_latest_measurement_at_or_before_t0(db):
    _holders(db, T0 - 7200, "token_details", top10_pct=10.0, holder_count=100)
    _holders(db, T0 - 600, "token_details", top10_pct=38.42, holder_count=947)
    f = features.holders_features(db, TOK, NET, T0)
    assert f["chain_top10_pct"] == 38.42
    assert f["chain_holder_count"] == 947
    assert f["holders_age_min"] == pytest.approx(10.0)


def test_each_source_keeps_its_own_latest_row(db):
    """The two sources measure two different things — one being newer does not
    erase the other."""
    _holders(db, T0 - 3600, "token_details", top10_pct=83.24, holder_count=937)
    _holders(db, T0 - 300, "hodlers_top", platform_holders=274,
             platform_holders_listed=50, platform_underwater=50,
             platform_value_usd=7990.77, platform_median_hold_seconds=24773,
             platform_dev_holding=0)
    f = features.holders_features(db, TOK, NET, T0)
    assert f["chain_top10_pct"] == 83.24              # not erased by the crowd row
    assert f["chain_holder_count"] == 937
    assert f["platform_holders"] == 274
    assert f["platform_value_usd"] == 7990.77
    assert f["holders_age_min"] == pytest.approx(5.0)  # the newer of the two stamps


def test_platform_penetration_needs_both_sources(db):
    """The platform's share of chain holders is a derivation no single source
    provides."""
    _holders(db, T0 - 300, "hodlers_top", platform_holders=274)
    assert features.holders_features(db, TOK, NET, T0)["platform_penetration"] is None

    _holders(db, T0 - 300, "token_details", top10_pct=83.24, holder_count=937)
    f = features.holders_features(db, TOK, NET, T0)
    assert f["platform_penetration"] == pytest.approx(274 / 937)


def test_penetration_none_when_chain_count_absent(db):
    """Division by an absent value ≠ zero — we do not fabricate a ratio
    (FR-007)."""
    _holders(db, T0 - 300, "token_details", top10_pct=50.0)   # holder_count absent
    _holders(db, T0 - 300, "hodlers_top", platform_holders=100)
    f = features.holders_features(db, TOK, NET, T0)
    assert f["chain_top10_pct"] == 50.0
    assert f["platform_holders"] == 100
    assert f["platform_penetration"] is None


def test_underwater_ratio_and_median_hold_hours(db):
    _holders(db, T0 - 300, "hodlers_top", platform_holders=274,
             platform_holders_listed=50, platform_underwater=12,
             platform_median_hold_seconds=36000)
    f = features.holders_features(db, TOK, NET, T0)
    assert f["platform_underwater_ratio"] == pytest.approx(0.24)
    assert f["platform_median_hold_h"] == pytest.approx(10.0)


def test_fully_underwater_crowd_is_one_not_missing(db):
    """All holders underwater = 1.0 (potential excess supply), not None."""
    _holders(db, T0 - 300, "hodlers_top", platform_holders=274,
             platform_holders_listed=49, platform_underwater=49)
    assert features.holders_features(db, TOK, NET, T0)["platform_underwater_ratio"] == 1.0


def test_no_holders_measurement_leaves_family_null(db):
    """Rows from before the cycle launched: NULL means "not measured", not
    "zero"."""
    f = features.holders_features(db, TOK, NET, T0)
    assert set(f) == {
        "chain_top10_pct", "chain_holder_count", "holders_age_min",
        "chain_holders_delta_1h", "chain_holders_growth_1h",
        "chain_holders_span_min",
        "platform_holders", "platform_penetration", "platform_underwater_ratio",
        "platform_value_usd", "platform_median_hold_h", "platform_dev_holding",
    }
    assert all(v is None for v in f.values())


def test_holders_of_other_token_do_not_leak(db):
    _holders(db, T0 - 300, "token_details", tok="0xother",
             top10_pct=99.0, holder_count=3)
    assert features.holders_features(db, TOK, NET, T0)["chain_top10_pct"] is None


# ---------------------------------------------------------------------------
# Ownership from the **chain** (onchain_*) — a measurement that never goes
# through FOMO.
#
# Solana used to be the only structural option (ERC-20 has no on-chain holder
# list), and since feature version 12 the `evm_layer` ledger writes into the
# **same** table ⇒ both networks together. So most tests here run on Solana,
# with a subsection at the end on EVM because the two `holder_count` columns
# come from the ledger alone.
# ---------------------------------------------------------------------------
SOLTOK, SOLNET = "SoLmint1111111111111111111111111111111111", "1399811149"


def _conc(db, epoch, tok=SOLTOK, net=SOLNET, **kw):
    row = {
        "token_address": tok, "network_id": net, "recorded_at": _iso(epoch),
        "watch_first_seen_at": _iso(T0 - 7200), "entry_signal_id": "sig-1",
        "is_control": 0, "supply": 1_000_000_000.0, "decimals": 6,
        "top1_pct": None, "top5_pct": None, "top10_pct": None, "top20_pct": None,
        "top_accounts": 20, "raw_json": "{}",
    }
    row.update(kw)
    db.insert_chain_concentration(row)
    return row


def test_onchain_features_read_latest_snapshot(db):
    _conc(db, T0 - 3600, top1_pct=5.0, top10_pct=20.0)          # older
    _conc(db, T0 - 120, top1_pct=32.21, top5_pct=46.31,
          top10_pct=53.16, top20_pct=60.99, top_accounts=20)
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_top1_pct"] == pytest.approx(32.21)
    assert f["onchain_top5_pct"] == pytest.approx(46.31)
    assert f["onchain_top10_pct"] == pytest.approx(53.16)
    assert f["onchain_top20_pct"] == pytest.approx(60.99)
    assert f["onchain_top_accounts"] == 20
    assert f["onchain_age_min"] == pytest.approx(2.0)


def test_onchain_snapshot_after_t0_never_leaks(db):
    """Leak guard: a measurement after the decision moment does not change
    the row."""
    _conc(db, T0 - 300, top1_pct=10.0, top10_pct=30.0)
    before = features.onchain_features(db, SOLTOK, SOLNET, T0)
    _conc(db, T0 + 60, top1_pct=90.0, top10_pct=99.0)           # the future
    assert features.onchain_features(db, SOLTOK, SOLNET, T0) == before


def test_onchain_delta_over_five_minutes(db):
    """The first five-minute window on whale movement: a whale grows 3 points
    in 5.5 minutes."""
    _conc(db, T0 - 390, top1_pct=29.0, top10_pct=50.0)
    _conc(db, T0 - 60, top1_pct=32.0, top10_pct=53.5)
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_top1_delta_5m"] == pytest.approx(3.0)
    assert f["onchain_top10_delta_5m"] == pytest.approx(3.5)
    assert f["onchain_delta_span_min"] == pytest.approx(5.5)


def test_onchain_delta_skips_snapshot_closer_than_four_minutes(db):
    """A snapshot less than 240s old is not "five minutes ago" — the query
    skips it for the one before it."""
    _conc(db, T0 - 420, top1_pct=20.0)      # 6 minutes before now — the reference
    _conc(db, T0 - 120, top1_pct=25.0)      # 2 minutes ago — too close
    _conc(db, T0 - 10, top1_pct=26.0)       # the present
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_top1_delta_5m"] == pytest.approx(6.0)      # 26 − 20
    assert f["onchain_delta_span_min"] == pytest.approx(410 / 60)


def test_onchain_delta_none_when_previous_snapshot_too_old(db):
    """A 20-minute gap is another window: the current ratios stay, and the
    delta is None, not a misleading number."""
    _conc(db, T0 - 1260, top1_pct=10.0)
    _conc(db, T0 - 60, top1_pct=40.0)
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_top1_pct"] == pytest.approx(40.0)
    assert f["onchain_top1_delta_5m"] is None
    assert f["onchain_delta_span_min"] is None


def test_onchain_unchanged_concentration_is_measured_zero(db):
    """Unchanged concentration = a measured zero, not "not measured" (FR-007)."""
    _conc(db, T0 - 390, top1_pct=15.0, top10_pct=40.0)
    _conc(db, T0 - 60, top1_pct=15.0, top10_pct=40.0)
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_top1_delta_5m"] == 0.0
    assert f["onchain_top10_delta_5m"] == 0.0


def test_onchain_burned_supply_keeps_row_with_null_ratios(db):
    """Zero supply: the row exists without ratios — and absence is not read
    as zero."""
    _conc(db, T0 - 60, supply=0.0, top_accounts=1)
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_top1_pct"] is None
    assert f["onchain_top_accounts"] == 1
    assert f["onchain_age_min"] == pytest.approx(1.0)


def test_no_onchain_measurement_leaves_family_null(db):
    """EVM rows and everything from before the layer launched: NULL means
    "cannot be measured", not zero."""
    f = features.onchain_features(db, TOK, NET, T0)
    assert set(f) == {
        "onchain_top1_pct", "onchain_top5_pct", "onchain_top10_pct",
        "onchain_top20_pct", "onchain_top_accounts", "onchain_age_min",
        "onchain_top1_delta_5m", "onchain_top10_delta_5m",
        "onchain_delta_span_min",
        # The two ledger columns (version 12): only the EVM ledger provides
        # them — Solana stays NULL in both because its source returns at most
        # 20 accounts, so there is no exact holder count.
        "onchain_holder_count", "onchain_holders_delta_5m",
    }
    assert all(v is None for v in f.values())


def test_onchain_of_other_token_does_not_leak(db):
    _conc(db, T0 - 60, tok="SoLother", top1_pct=99.0)
    assert features.onchain_features(db, SOLTOK, SOLNET, T0)["onchain_top1_pct"] is None


def test_onchain_family_is_in_feature_columns(db):
    """A column missing from FEATURE_COLUMNS is computed then silently
    dropped — the guard is here."""
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert set(f) <= set(features.FEATURE_COLUMNS)


# --- the ledger columns: EVM only (Solana caps at 20 accounts, so no total) ---
def test_onchain_holder_count_comes_from_the_evm_ledger(db):
    """`holder_count` is exact because the ledger knows every address, not
    just the top twenty."""
    _conc(db, T0 - 60, tok=TOK, net=NET, top1_pct=12.5, top_accounts=20,
          holder_count=1_843)
    f = features.onchain_features(db, TOK, NET, T0)
    assert f["onchain_holder_count"] == 1_843
    assert f["onchain_top_accounts"] == 20          # rank ≠ total


def test_onchain_holders_delta_over_five_minutes(db):
    """The first five-minute window on the project's holder **count** (FOMO's
    cadence is 25m)."""
    _conc(db, T0 - 390, tok=TOK, net=NET, holder_count=1_800)
    _conc(db, T0 - 60, tok=TOK, net=NET, holder_count=1_843)
    f = features.onchain_features(db, TOK, NET, T0)
    assert f["onchain_holders_delta_5m"] == 43
    assert f["onchain_delta_span_min"] == pytest.approx(5.5)


def test_onchain_holders_delta_can_be_negative(db):
    """Holders leaving is as much information as holders arriving: the sign is
    kept, not clamped to zero."""
    _conc(db, T0 - 390, tok=TOK, net=NET, holder_count=900)
    _conc(db, T0 - 60, tok=TOK, net=NET, holder_count=870)
    assert features.onchain_features(db, TOK, NET, T0)["onchain_holders_delta_5m"] == -30


def test_onchain_holders_delta_null_when_solana_has_no_count(db):
    """Solana: ratios are measured but the count is not ⇒ the column is NULL,
    not zero (FR-007)."""
    _conc(db, T0 - 390, top1_pct=20.0)
    _conc(db, T0 - 60, top1_pct=25.0)
    f = features.onchain_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_top1_delta_5m"] == pytest.approx(5.0)
    assert f["onchain_holder_count"] is None
    assert f["onchain_holders_delta_5m"] is None


# ---------------------------------------------------------------------------
# EVM contract shape and authorities (onchain_contract_*) — from the bytecode,
# Base only.
#
# An hourly cadence, so age is measured in tens of minutes, not units. And the
# critical distinction is the same as `evm_contract`: a flag absent from a read
# contract = a **measured zero**; a contract never scanned = None.
# ---------------------------------------------------------------------------
BASETOK, BASENET = "0xbase01", "8453"


def _contract(db, epoch, tok=BASETOK, net=BASENET, **kw):
    row = {
        "token_address": tok, "network_id": net, "recorded_at": _iso(epoch),
        "watch_first_seen_at": _iso(T0 - 7200), "entry_signal_id": "sig-1",
        "is_control": 0, "code_size": 4_096, "code_hash": "0x" + "ab" * 32,
        "function_count": 37, "is_proxy": 0, "impl_address": None,
        "owner_address": None, "is_ownership_renounced": None,
        "has_mint": 0, "has_pause": 0, "has_blacklist": 0, "has_fee_setter": 0,
        "has_limit_setter": 0, "has_trading_switch": 0, "raw_json": "{}",
    }
    row.update(kw)
    db.insert_evm_contract(row)
    return row


def test_contract_features_read_latest_scan(db):
    _contract(db, T0 - 7_000, code_size=1, function_count=1)      # older
    _contract(db, T0 - 3_300, code_size=14_812, function_count=61,
              has_mint=1, has_limit_setter=1, is_ownership_renounced=0)
    f = features.onchain_contract_features(db, BASETOK, BASENET, T0)
    assert f["onchain_code_size"] == 14_812
    assert f["onchain_function_count"] == 61
    assert f["onchain_has_mint_fn"] == 1
    assert f["onchain_has_limit_setter"] == 1
    assert f["onchain_has_pause_fn"] == 0            # read and absent = a measured zero
    assert f["onchain_owner_renounced"] == 0
    assert f["onchain_contract_age_min"] == pytest.approx(55.0)


def test_contract_scan_after_t0_never_leaks(db):
    """Leak guard: an ownership renunciation after the decision moment does
    not enter the row."""
    _contract(db, T0 - 600, is_ownership_renounced=0)
    before = features.onchain_contract_features(db, BASETOK, BASENET, T0)
    _contract(db, T0 + 60, is_ownership_renounced=1)
    assert features.onchain_contract_features(db, BASETOK, BASENET, T0) == before


def test_contract_owner_renounced_null_is_not_renounced(db):
    """"no owner function" ≠ "ownership renounced" — and the column preserves
    the difference."""
    _contract(db, T0 - 600, owner_address=None, is_ownership_renounced=None)
    f = features.onchain_contract_features(db, BASETOK, BASENET, T0)
    assert f["onchain_owner_renounced"] is None
    assert f["onchain_code_size"] == 4_096          # and the contract was indeed read


def test_contract_non_contract_address_is_a_measured_zero(db):
    """`0x` ⇒ not a contract: a zero size is a measurement, and the function
    count stays NULL, not zero."""
    _contract(db, T0 - 600, code_size=0, code_hash=None, function_count=None,
              has_mint=None, has_pause=None, has_blacklist=None,
              has_fee_setter=None, has_limit_setter=None, has_trading_switch=None)
    f = features.onchain_contract_features(db, BASETOK, BASENET, T0)
    assert f["onchain_code_size"] == 0
    assert f["onchain_function_count"] is None
    assert f["onchain_has_mint_fn"] is None


def test_contract_proxy_flag_survives(db):
    """On a proxy, every danger flag below means nothing — so the flag itself
    must come through."""
    _contract(db, T0 - 600, code_size=45, is_proxy=1,
              impl_address="0x" + "12" * 20, function_count=0)
    f = features.onchain_contract_features(db, BASETOK, BASENET, T0)
    assert f["onchain_is_proxy"] == 1
    assert f["onchain_function_count"] == 0


def test_no_contract_scan_leaves_family_null(db):
    """BSC and Robinhood (and everything from before the layer): NULL means
    "never scanned", not zero."""
    f = features.onchain_contract_features(db, TOK, NET, T0)
    assert set(f) == {
        "onchain_code_size", "onchain_function_count", "onchain_is_proxy",
        "onchain_owner_renounced", "onchain_has_mint_fn", "onchain_has_pause_fn",
        "onchain_has_blacklist_fn", "onchain_has_fee_setter",
        "onchain_has_limit_setter", "onchain_has_trading_switch",
        "onchain_contract_age_min",
    }
    assert all(v is None for v in f.values())


def test_contract_of_other_token_does_not_leak(db):
    _contract(db, T0 - 600, tok="0xother", has_mint=1)
    assert features.onchain_contract_features(
        db, BASETOK, BASENET, T0
    )["onchain_has_mint_fn"] is None


def test_contract_family_is_in_feature_columns(db):
    """A column missing from FEATURE_COLUMNS is computed then silently
    dropped — the guard is here."""
    _contract(db, T0 - 600)
    f = features.onchain_contract_features(db, BASETOK, BASENET, T0)
    assert set(f) <= set(features.FEATURE_COLUMNS)


# ---------------------------------------------------------------------------
# Authorities from the chain (onchain_has_* / is_mutable / dev) — the slow
# layer.
#
# A separate queue and an hourly cadence, so age here is measured in hours,
# not minutes. And the critical distinction: a `has_*` flag becomes a
# **measured zero** when a row exists with a NULL authority (the authority is
# actually revoked), and stays None when there is no row at all (not measured).
# ---------------------------------------------------------------------------
def _auth(db, epoch, tok=SOLTOK, net=SOLNET, **kw):
    row = {
        "token_address": tok, "network_id": net, "recorded_at": _iso(epoch),
        "watch_first_seen_at": _iso(T0 - 7200), "entry_signal_id": "sig-1",
        "is_control": 0, "token_program": "spl-token-2022",
        "mint_authority": None, "freeze_authority": None, "update_authority": None,
        "is_mutable": None, "creator_address": None, "creator_count": None,
        "supply": 999673699.453014, "decimals": 6,
        "dev_owner": None, "dev_holding_pct": None, "raw_json": "{}",
    }
    row.update(kw)
    db.insert_chain_authority(row)
    return row


def test_onchain_authority_reads_latest_row(db):
    _auth(db, T0 - 7200, mint_authority="Old", is_mutable=1)     # older
    _auth(db, T0 - 1800, mint_authority="DevWa11et", freeze_authority="FrzAuth",
          is_mutable=1, dev_holding_pct=4.25)
    f = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_has_mint_authority"] == 1
    assert f["onchain_has_freeze_authority"] == 1
    assert f["onchain_is_mutable"] == 1
    assert f["onchain_is_token2022"] == 1
    assert f["onchain_dev_holding_pct"] == pytest.approx(4.25)
    assert f["onchain_auth_age_min"] == pytest.approx(30.0)


def test_onchain_authority_row_after_t0_never_leaks(db):
    _auth(db, T0 - 1800, mint_authority="Now")
    before = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    _auth(db, T0 + 60, mint_authority="Future", is_mutable=1, dev_holding_pct=99.0)
    assert features.onchain_authority_features(db, SOLTOK, SOLNET, T0) == before


def test_onchain_revoked_authority_is_measured_zero(db):
    """The dominant case (45 of 48): the authority is revoked ⇒ a measured
    zero, not None (FR-007)."""
    _auth(db, T0 - 600, is_mutable=0)
    f = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_has_mint_authority"] == 0
    assert f["onchain_has_freeze_authority"] == 0
    assert f["onchain_is_mutable"] == 0


def test_onchain_mutable_stays_null_when_das_failed(db):
    """One absent column does not kill the rest of the row, and it is not
    read as "not mutable"."""
    _auth(db, T0 - 600, is_mutable=None)
    f = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_is_mutable"] is None
    assert f["onchain_has_mint_authority"] == 0       # and this one is truly measured
    assert f["onchain_dev_holding_pct"] is None


def test_onchain_legacy_token_program_flag_is_zero(db):
    _auth(db, T0 - 600, token_program="spl-token")
    f = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_is_token2022"] == 0


def test_onchain_dev_holding_zero_survives(db):
    """The developer sold everything = information, not absence."""
    _auth(db, T0 - 600, dev_owner="Dev1", dev_holding_pct=0.0)
    f = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_dev_holding_pct"] == 0.0


def test_no_authority_measurement_leaves_family_null(db):
    f = features.onchain_authority_features(db, TOK, NET, T0)
    assert set(f) == {
        "onchain_has_mint_authority", "onchain_has_freeze_authority",
        "onchain_is_mutable", "onchain_is_token2022",
        "onchain_dev_holding_pct", "onchain_auth_age_min",
    }
    assert all(v is None for v in f.values())


def test_authority_of_other_token_does_not_leak(db):
    _auth(db, T0 - 600, tok="SoLother", mint_authority="X", dev_holding_pct=50.0)
    f = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    assert f["onchain_has_mint_authority"] is None
    assert f["onchain_dev_holding_pct"] is None


def test_authority_family_is_in_feature_columns(db):
    f = features.onchain_authority_features(db, SOLTOK, SOLNET, T0)
    assert set(f) <= set(features.FEATURE_COLUMNS)


def test_authority_family_reaches_build_features(db):
    """The function may be computed yet never reach build_features — the guard
    is here."""
    _auth(db, T0 - 600, mint_authority="DevWa11et", is_mutable=1)
    event = {"signal_type": "large_buy", "occurred_at": _iso(T0),
             "token_address": SOLTOK, "network_id": SOLNET}
    f = features.build_features(db, event, SOLTOK, SOLNET, T0)
    assert f["onchain_has_mint_authority"] == 1
    assert f["onchain_auth_age_min"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# External legitimacy — it sat in raw_json from day one with no extraction
# ---------------------------------------------------------------------------
def _static(db, epoch=None, **kw):
    row = {
        "token_address": TOK, "network_id": NET,
        "recorded_at": _iso(T0 - 3600 if epoch is None else epoch),
        "name": "A", "symbol": "A", "decimals": 18, "mintable": None,
        "freezable": None, "is_scam": None, "creator_address": None,
        "launchpad_name": None, "migrated": None, "graduation_percent": None,
        "twitter": None, "telegram": None, "website": None, "discord": None,
        "token_created_at": None, "exchanges_count": None,
        "exchanges_json": None, "cmc_id": None, "description": None,
        "description_len": None, "has_banner": None, "has_image": None,
        "raw_json": "{}",
    }
    row.update(kw)
    db.upsert_static(row)
    return row


def test_listed_on_exchange_derived_from_count(db):
    _static(db, exchanges_count=8, cmc_id="1839", description_len=120, has_banner=1)
    f = features.static_features(db, TOK, NET, T0)
    assert f["exchanges_count"] == 8
    assert f["listed_on_exchange"] == 1
    assert f["has_cmc_id"] == 1
    assert f["description_len"] == 120
    assert f["has_banner"] == 1


def test_zero_exchanges_is_measured_zero_not_missing(db):
    """"checked and no exchange" differs from "not checked" — a true zero,
    not None."""
    _static(db, exchanges_count=0)
    f = features.static_features(db, TOK, NET, T0)
    assert f["exchanges_count"] == 0
    assert f["listed_on_exchange"] == 0


def test_absent_exchange_count_stays_unknown(db):
    """Absent ≠ zero (FR-007): an old snapshot without the column stays
    unknown, not zero."""
    _static(db, exchanges_count=None)
    f = features.static_features(db, TOK, NET, T0)
    assert f["exchanges_count"] is None
    assert f["listed_on_exchange"] is None


def test_snapshot_without_cmc_id_is_measured_absence(db):
    """A snapshot present without cmc_id = "checked and not listed" ⇒ zero,
    not unknown."""
    _static(db, cmc_id=None)
    assert features.static_features(db, TOK, NET, T0)["has_cmc_id"] == 0


def test_legitimacy_unknown_before_any_snapshot(db):
    """With no snapshot at all nothing is measured — every flag is unknown,
    including the boolean ones."""
    f = features.static_features(db, TOK, NET, T0)
    for key in ("exchanges_count", "listed_on_exchange", "has_cmc_id",
                "description_len", "has_banner"):
        assert f[key] is None, key


def test_legitimacy_snapshot_after_t0_is_invisible(db):
    """Leak guard: an exchange listing after the signal is not known at it."""
    _static(db, epoch=T0 + 600, exchanges_count=8, cmc_id="1839", has_banner=1)
    f = features.static_features(db, TOK, NET, T0)
    assert f["exchanges_count"] is None
    assert f["listed_on_exchange"] is None
    assert f["has_cmc_id"] is None
