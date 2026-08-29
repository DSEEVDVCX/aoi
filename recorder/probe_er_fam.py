"""Family definitions taken verbatim from the section comments in features.FEATURE_COLUMNS."""

FAMILIES = {
    "trigger_event": [
        "signal_type", "size_usd", "in_amount", "out_amount", "token_amount",
        "avg_cost", "price_to_avg_cost", "realized_pnl_usd", "num_swaps",
        "is_first_buy", "buyer_pnl_pct", "market_cap", "fdv", "price_usd",
        "log_market_cap", "log_size_usd", "size_to_mcap", "unique_traders",
        "num_trades", "minutes", "price_change_pct", "total_volume",
        "volume_per_trader", "are_top_traders", "top_trader_match_count",
        "top_traders_listed", "top_trader_match_ratio",
        "buyers_best_rank", "rank_le_10", "rank_le_50",
        "ticker_len", "ticker_has_digit", "ticker_non_ascii", "hour_utc", "dow",
    ],
    "top_trader_periods": [
        "top_trader_match_count_24h", "buyers_best_rank_24h",
        "top_trader_match_count_7d", "buyers_best_rank_7d",
        "top_trader_match_count_30d", "buyers_best_rank_30d",
        "top_trader_periods_matched", "top_trader_any_period", "best_rank_any_period",
    ],
    "token_static": [
        "token_age_h", "launchpad_name", "migrated", "graduation_percent", "is_scam",
        "mintable", "freezable", "socials_count", "has_twitter", "creator_prior_tokens",
        "name_len", "name_non_ascii", "decimals",
        "exchanges_count", "listed_on_exchange", "has_cmc_id", "description_len",
        "has_banner",
    ],
    "social_thesis": [
        "thesis_counted", "thesis_counted_capped", "thesis_authors_before",
        "thesis_1h", "thesis_24h", "thesis_accel", "hours_since_last_thesis",
        "thesis_history_days",
        "social_thesis_total", "social_thesis_authors", "social_holder_authors",
        "social_holder_ratio", "social_replies", "social_snapshot_age_min",
        "social_total_delta_1h", "social_total_growth_1h", "social_authors_delta_1h",
    ],
    "price_path": [
        "ret_1h_before", "ret_4h_before", "ret_24h_before", "ret_7d_before",
        "vol_24h_before", "flat_ratio_24h", "up_candle_ratio_24h", "dist_from_ath",
        "ath_history_complete", "ath_history_days",
        "bars_history_h", "bars_count_24h", "bar_vol_1h", "bar_vol_24h",
        "vol_surge_1h",
    ],
    "market_snapshot": [
        "liquidity", "holders", "top10_holders_pct", "volume_24h", "buy_count_24h",
        "sell_count_24h", "buy_sell_ratio_24h", "unique_buys_24h", "unique_sells_24h",
        "tick_age_min", "tick_change_1h", "tick_change_4h", "tick_change_24h",
        "tick_volume_1h", "tick_volume_4h", "tick_txn_1h", "tick_txn_24h",
        "volume_to_liquidity", "liquidity_to_mcap", "float_ratio", "tick_rich_age_min",
    ],
    "chain_ownership": [
        "chain_top10_pct", "chain_holder_count", "holders_age_min",
        "chain_holders_delta_1h", "chain_holders_growth_1h", "chain_holders_span_min",
        "platform_holders", "platform_penetration", "platform_underwater_ratio",
        "platform_value_usd", "platform_median_hold_h", "platform_dev_holding",
    ],
    "onchain_concentration": [
        "onchain_top1_pct", "onchain_top5_pct", "onchain_top10_pct",
        "onchain_top20_pct", "onchain_top_accounts", "onchain_age_min",
        "onchain_top1_delta_5m", "onchain_top10_delta_5m", "onchain_delta_span_min",
        "onchain_holder_count", "onchain_holders_delta_5m",
    ],
    "onchain_sol_auth": [
        "onchain_has_mint_authority", "onchain_has_freeze_authority",
        "onchain_is_mutable", "onchain_is_token2022", "onchain_dev_holding_pct",
        "onchain_auth_age_min",
    ],
    "onchain_evm_contract": [
        "onchain_code_size", "onchain_function_count", "onchain_is_proxy",
        "onchain_owner_renounced", "onchain_has_mint_fn", "onchain_has_pause_fn",
        "onchain_has_blacklist_fn", "onchain_has_fee_setter",
        "onchain_has_limit_setter", "onchain_has_trading_switch",
        "onchain_contract_age_min",
    ],
    "flow": [
        "flow_age_min", "flow_buy_volume_5m", "flow_sell_volume_5m",
        "flow_net_volume_5m", "flow_net_volume_1h", "flow_net_volume_24h",
        "flow_buy_sell_volume_ratio_5m", "flow_buy_sell_volume_ratio_1h",
        "flow_buy_sell_volume_ratio_24h",
        "flow_buy_count_5m", "flow_sell_count_5m",
        "flow_unique_buys_5m", "flow_unique_sells_5m",
        "flow_buy_sell_count_ratio_5m", "flow_unique_ratio_5m",
        "flow_trade_size_5m", "flow_is_low_fees",
    ],
    "macro_regime": ["sol_ret_4h", "sol_ret_24h", "eth_ret_24h"],
    "signal_density": ["prior_signals_token", "minutes_since_prior_signal", "global_signals_1h"],
    "labels": [
        "final_return_48h", "max_gain_1h", "max_gain_4h", "max_gain_24h",
        "max_gain_48h", "max_drawdown_48h", "time_to_peak_h", "is_rug",
    ],
}

# the one column per family that only a collector can set (its "witness"):
# NULL here == that collector never produced a row at/before t0 for this token.
WITNESS = {
    "top_trader_periods": "top_trader_periods_matched",
    "token_static": "decimals",
    "social_thesis": "social_snapshot_age_min",
    "price_path": "bars_history_h",
    "market_snapshot": "tick_age_min",
    "chain_ownership": "holders_age_min",
    "onchain_concentration": "onchain_age_min",
    "onchain_sol_auth": "onchain_auth_age_min",
    "onchain_evm_contract": "onchain_contract_age_min",
    "flow": "flow_age_min",
    "macro_regime": "sol_ret_24h",
    "signal_density": "global_signals_1h",
    "labels": "final_return_48h",
    "trigger_event": "market_cap",
}

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = (
    "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
    "AND is_independent=1 AND feature_version = " + FV
)


def empty_expr(fam: str) -> str:
    return "(" + " AND ".join(f"{c} IS NULL" for c in FAMILIES[fam]) + ")"


def n_empty_expr(names=None) -> str:
    names = names or list(FAMILIES)
    return " + ".join(f"CASE WHEN {empty_expr(f)} THEN 1 ELSE 0 END" for f in names)
