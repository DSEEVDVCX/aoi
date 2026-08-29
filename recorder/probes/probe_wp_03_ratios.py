import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
W = f"""kind='signal' AND is_live=1 AND asset_class='meme'
        AND status='ok' AND is_independent=1 AND feature_version = {FV}"""

def one(sql, args=()):
    return con.execute(sql, args).fetchone()

print("=== A. _log1p(v<=0) -> NULL : measured zero becomes absence ===")
r = one(f"""SELECT COUNT(*) n,
   SUM(CASE WHEN size_usd = 0 THEN 1 ELSE 0 END) size_zero,
   SUM(CASE WHEN size_usd = 0 AND log_size_usd IS NULL THEN 1 ELSE 0 END) size_zero_lognull,
   SUM(CASE WHEN size_usd < 0 THEN 1 ELSE 0 END) size_neg,
   SUM(CASE WHEN market_cap = 0 THEN 1 ELSE 0 END) mcap_zero,
   SUM(CASE WHEN market_cap = 0 AND log_market_cap IS NULL THEN 1 ELSE 0 END) mcap_zero_lognull,
   SUM(CASE WHEN size_usd IS NULL THEN 1 ELSE 0 END) size_null,
   SUM(CASE WHEN log_size_usd IS NULL THEN 1 ELSE 0 END) log_size_null,
   SUM(CASE WHEN market_cap IS NULL THEN 1 ELSE 0 END) mcap_null,
   SUM(CASE WHEN log_market_cap IS NULL THEN 1 ELSE 0 END) log_mcap_null
   FROM training_rows WHERE {W}""")
print(dict(r))

print("\n=== B. ratio denominators measured as 0 -> ratio NULL ===")
pairs = [
    ("buy_sell_ratio_24h", "buy_count_24h", "sell_count_24h"),
    ("volume_to_liquidity", "volume_24h", "liquidity"),
    ("liquidity_to_mcap", "liquidity", "market_cap"),
    ("float_ratio", None, None),
    ("volume_per_trader", "total_volume", "unique_traders"),
    ("size_to_mcap", "size_usd", "market_cap"),
    ("flow_buy_sell_volume_ratio_5m", "flow_buy_volume_5m", "flow_sell_volume_5m"),
    ("flow_buy_sell_count_ratio_5m", "flow_buy_count_5m", "flow_sell_count_5m"),
    ("flow_unique_ratio_5m", "flow_unique_buys_5m", "flow_buy_count_5m"),
    ("flow_trade_size_5m", "flow_buy_volume_5m", "flow_buy_count_5m"),
    ("social_holder_ratio", "social_holder_authors", "social_thesis_authors"),
    ("platform_penetration", "platform_holders", "chain_holder_count"),
    ("thesis_accel", "thesis_1h", "thesis_24h"),
    ("vol_surge_1h", "bar_vol_1h", "bar_vol_24h"),
    ("top_trader_match_ratio", "top_trader_match_count", "top_traders_listed"),
]
for ratio, num, den in pairs:
    if num is None:
        continue
    r = one(f"""SELECT
       SUM(CASE WHEN {ratio} IS NULL THEN 1 ELSE 0 END) ratio_null,
       SUM(CASE WHEN {den} = 0 THEN 1 ELSE 0 END) den_zero,
       SUM(CASE WHEN {den} = 0 AND {num} IS NOT NULL THEN 1 ELSE 0 END) den_zero_num_known,
       SUM(CASE WHEN {den} = 0 AND {num} > 0 THEN 1 ELSE 0 END) den_zero_num_pos,
       SUM(CASE WHEN {den} = 0 AND {num} = 0 THEN 1 ELSE 0 END) both_zero,
       SUM(CASE WHEN {ratio} = 0 THEN 1 ELSE 0 END) ratio_zero
       FROM training_rows WHERE {W}""")
    print(f"{ratio:32s} null={r['ratio_null']:6d} den0={r['den_zero']:6d} "
          f"den0&num_known={r['den_zero_num_known']:6d} den0&num>0={r['den_zero_num_pos']:6d} "
          f"both0={r['both_zero']:6d} ratio=0:{r['ratio_zero']}")

print("\n=== C. strftime('%s', col) NULL rate on the timestamp cols features.py reads in SQL ===")
checks = [
    ("token_thesis", "created_at"),
    ("signal_events", "ts"),
    ("activity_events", "ts"),
    ("market_ticks", "recorded_at"),
    ("token_social", "recorded_at"),
    ("token_holders", "recorded_at"),
    ("chain_concentration", "recorded_at"),
    ("chain_authority", "recorded_at"),
    ("evm_contract", "recorded_at"),
    ("token_flow", "recorded_at"),
    ("token_static", "recorded_at"),
]
for t, c in checks:
    try:
        r = one(f"""SELECT COUNT(*) n,
                     SUM(CASE WHEN {c} IS NOT NULL AND strftime('%s', {c}) IS NULL
                              THEN 1 ELSE 0 END) bad,
                     SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) nulls FROM {t}""")
        print(f"{t}.{c:14s} rows={r['n']:9d} unparseable={r['bad']:8d} null={r['nulls']}")
    except Exception as e:
        print(f"{t}.{c}: FAILED {e}")

print("\n=== D. token_static.token_created_at format distribution ===")
r = one("""SELECT COUNT(*) n,
    SUM(CASE WHEN token_created_at IS NULL THEN 1 ELSE 0 END) nulls,
    SUM(CASE WHEN token_created_at GLOB '[0-9]*' AND token_created_at NOT GLOB '*-*'
             THEN 1 ELSE 0 END) numericish,
    SUM(CASE WHEN token_created_at LIKE '%Z' THEN 1 ELSE 0 END) iso_z,
    SUM(CASE WHEN token_created_at LIKE '%+00:00' THEN 1 ELSE 0 END) iso_off,
    SUM(CASE WHEN token_created_at GLOB '*-*T*' AND token_created_at NOT LIKE '%Z'
             AND token_created_at NOT LIKE '%+%' THEN 1 ELSE 0 END) iso_naive
    FROM token_static""")
print(dict(r))
print("samples:", [x[0] for x in con.execute(
    "SELECT DISTINCT token_created_at FROM token_static WHERE token_created_at IS NOT NULL LIMIT 6")])

print("\n=== E. platform_holders_listed cross-tab (extract.py:1044) ===")
r = one("""SELECT COUNT(*) n,
   SUM(CASE WHEN platform_holders=0 AND platform_holders_listed IS NULL THEN 1 ELSE 0 END) t0_lnull,
   SUM(CASE WHEN platform_holders>0 AND platform_holders_listed IS NULL THEN 1 ELSE 0 END) tpos_lnull,
   SUM(CASE WHEN platform_holders=0 AND platform_value_usd IS NULL THEN 1 ELSE 0 END) t0_valnull,
   SUM(CASE WHEN platform_holders=0 AND platform_dev_holding IS NULL THEN 1 ELSE 0 END) t0_devnull
   FROM token_holders WHERE source='hodlers_top'""")
print(dict(r))
con.close()
