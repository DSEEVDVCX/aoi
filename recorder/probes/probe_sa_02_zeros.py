import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = (f"FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme' "
       f"AND status='ok' AND is_independent=1 AND feature_version = {FV}")

n = con.execute("SELECT COUNT(*) c " + POP).fetchone()["c"]
print("model population =", n)

# ---- A. fabricated-zero suspects: counts of 0 / NULL / >0 -------------------
print("\n=== A. suspect fabricated zeros (features.py sum()/len()/truthiness) ===")
cols = ["thesis_counted", "thesis_authors_before", "thesis_1h", "thesis_24h",
        "thesis_counted_capped", "socials_count", "has_twitter", "has_cmc_id",
        "description_len", "has_banner", "listed_on_exchange", "exchanges_count",
        "ath_history_complete", "creator_prior_tokens", "top_traders_listed"]
parts = []
for c in cols:
    parts.append(f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) n_{c}")
    parts.append(f"SUM(CASE WHEN {c}=0 THEN 1 ELSE 0 END) z_{c}")
    parts.append(f"SUM(CASE WHEN {c}>0 THEN 1 ELSE 0 END) p_{c}")
r = con.execute("SELECT " + ", ".join(parts) + " " + POP).fetchone()
for c in cols:
    print(f"  {c:24s} null={r['n_'+c]:6d}  zero={r['z_'+c]:6d}  pos={r['p_'+c]:6d}")

# ---- B. decisive: thesis family zero while the snapshot proves theses exist --
print("\n=== B. thesis_counted=0 but token_social snapshot says theses exist ===")
r = con.execute(f"""SELECT
   SUM(CASE WHEN thesis_counted=0 THEN 1 ELSE 0 END) tc0,
   SUM(CASE WHEN thesis_counted=0 AND social_thesis_total > 0 THEN 1 ELSE 0 END) tc0_but_real,
   SUM(CASE WHEN thesis_counted=0 AND social_thesis_total = 0 THEN 1 ELSE 0 END) tc0_and_zero,
   SUM(CASE WHEN thesis_counted=0 AND social_thesis_total IS NULL THEN 1 ELSE 0 END) tc0_unknown,
   MAX(CASE WHEN thesis_counted=0 THEN social_thesis_total END) worst,
   SUM(CASE WHEN thesis_counted=0 AND social_thesis_total>0 THEN social_thesis_total ELSE 0 END) lost_theses,
   SUM(CASE WHEN thesis_authors_before=0 AND social_thesis_authors>0 THEN 1 ELSE 0 END) authors0_but_real
   {POP}""").fetchone()
print(dict(r))

# ---- C. socials_count / has_twitter=0 while the static row had no social block
print("\n=== C. socials_count=0 cross-tab with has_twitter / description_len ===")
r = con.execute(f"""SELECT
   SUM(CASE WHEN socials_count=0 THEN 1 ELSE 0 END) sc0,
   SUM(CASE WHEN socials_count=0 AND has_twitter=0 AND description_len=0
             AND has_banner=0 AND has_cmc_id=0 THEN 1 ELSE 0 END) all_five_zero,
   SUM(CASE WHEN socials_count IS NOT NULL AND description_len=0 THEN 1 ELSE 0 END) dl0,
   SUM(CASE WHEN listed_on_exchange IS NULL THEN 1 ELSE 0 END) loe_null,
   SUM(CASE WHEN description_len IS NULL THEN 1 ELSE 0 END) dl_null,
   SUM(CASE WHEN has_cmc_id IS NULL THEN 1 ELSE 0 END) cmc_null
   {POP}""").fetchone()
print(dict(r))

# ---- D. ratio denominators that are a MEASURED zero -> ratio lands NULL -----
print("\n=== D. measured-zero denominator -> ratio NULL (numerator present) ===")
PAIRS = [
    ("buy_sell_ratio_24h",             "buy_count_24h",      "sell_count_24h"),
    ("volume_per_trader",              "total_volume",       "unique_traders"),
    ("size_to_mcap",                   "size_usd",           "market_cap"),
    ("vol_surge_1h",                   "bar_vol_1h",         "bar_vol_24h"),
    ("social_holder_ratio",            "social_holder_authors", "social_thesis_authors"),
    ("platform_penetration",           "platform_holders",   "chain_holder_count"),
    ("volume_to_liquidity",            "volume_24h",         "liquidity"),
    ("top_trader_match_ratio",         "top_trader_match_count", "top_traders_listed"),
    ("thesis_accel",                   "thesis_1h",          "thesis_24h"),
    ("price_to_avg_cost",              "price_usd",          "avg_cost"),
    ("liquidity_to_mcap",              "liquidity",          "market_cap"),
    ("flow_buy_sell_volume_ratio_5m",  "flow_buy_volume_5m", "flow_sell_volume_5m"),
    ("flow_buy_sell_count_ratio_5m",   "flow_buy_count_5m",  "flow_sell_count_5m"),
    ("flow_unique_ratio_5m",           "flow_unique_buys_5m","flow_buy_count_5m"),
    ("flow_trade_size_5m",             "flow_buy_volume_5m", "flow_buy_count_5m"),
    ("platform_underwater_ratio",      "platform_holders",   "platform_holders"),
]
sel = []
for ratio, num, den in PAIRS:
    sel.append(f"SUM(CASE WHEN {ratio} IS NULL AND {num} IS NOT NULL AND {den}=0 "
               f"THEN 1 ELSE 0 END) k_{ratio}")
    sel.append(f"SUM(CASE WHEN {den}=0 THEN 1 ELSE 0 END) d0_{ratio}")
    sel.append(f"SUM(CASE WHEN {ratio} IS NOT NULL THEN 1 ELSE 0 END) ok_{ratio}")
r = con.execute("SELECT " + ", ".join(sel) + " " + POP).fetchone()
for ratio, num, den in PAIRS:
    print(f"  {ratio:30s} den({den})=0 in {r['d0_'+ratio]:6d}"
          f" | ratio NULL *because* den=0 with num present: {r['k_'+ratio]:6d}"
          f" | ratio non-null: {r['ok_'+ratio]:6d}")

# ---- E. log_market_cap / log_size_usd on a measured zero --------------------
print("\n=== E. _log1p on a measured zero (v<=0 -> None) ===")
r = con.execute(f"""SELECT
  SUM(CASE WHEN market_cap=0 THEN 1 ELSE 0 END) mc0,
  SUM(CASE WHEN market_cap=0 AND log_market_cap IS NULL THEN 1 ELSE 0 END) mc0_lognull,
  SUM(CASE WHEN market_cap IS NULL THEN 1 ELSE 0 END) mcnull,
  SUM(CASE WHEN size_usd=0 THEN 1 ELSE 0 END) sz0,
  SUM(CASE WHEN size_usd=0 AND log_size_usd IS NULL THEN 1 ELSE 0 END) sz0_lognull,
  SUM(CASE WHEN size_usd IS NULL THEN 1 ELSE 0 END) sznull,
  SUM(CASE WHEN log_market_cap IS NULL THEN 1 ELSE 0 END) lmc_null
  {POP}""").fetchone()
print(dict(r))

# ---- F. ath_history_complete=0 with and without bars -----------------------
print("\n=== F. ath_history_complete=0 vs presence of bars ===")
r = con.execute(f"""SELECT
  SUM(CASE WHEN ath_history_complete=0 THEN 1 ELSE 0 END) c0,
  SUM(CASE WHEN ath_history_complete=0 AND bars_history_h IS NULL THEN 1 ELSE 0 END) c0_nobars,
  SUM(CASE WHEN ath_history_complete=0 AND bars_history_h IS NOT NULL THEN 1 ELSE 0 END) c0_hasbars,
  SUM(CASE WHEN ath_history_complete=1 THEN 1 ELSE 0 END) c1,
  SUM(CASE WHEN ath_history_complete IS NULL THEN 1 ELSE 0 END) cnull
  {POP}""").fetchone()
print(dict(r))
con.close()
