import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("features.FEATURE_VERSION =", features.FEATURE_VERSION)
for k in ("current_feature_version",):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    print(k, "=", r["value"] if r else None)

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

n = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP}").fetchone()["c"]
print("\npopulation (base table, meta fv):", n)

# population at hardcoded fv=12 for comparison
n12 = con.execute("""SELECT COUNT(*) c FROM training_rows WHERE kind='signal' AND is_live=1
   AND asset_class='meme' AND status='ok' AND is_independent=1 AND feature_version=12""").fetchone()["c"]
print("population at feature_version=12:", n12)

# distribution of feature_version within the live/meme/ok/indep cut
print("\nfeature_version histogram in live meme ok indep signal rows:")
for r in con.execute("""SELECT feature_version fv, COUNT(*) c FROM training_rows
   WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
   GROUP BY 1 ORDER BY 1"""):
    print("  fv", r["fv"], r["c"])

# ---- my own test: tick_age_min is non-NULL whenever ANY tick row exists at<=t0
# (features.market_features computes it unconditionally from rows[0]) so
# tick_age_min IS NULL <=> no market_ticks row at or before entry_ts.
q1 = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP} AND tick_age_min IS NULL").fetchone()["c"]
print("\nMY TEST market: tick_age_min IS NULL =", q1, f"({100*q1/n:.2f}%)")

# their 21-column AND
theirs = """liquidity IS NULL AND holders IS NULL AND volume_24h IS NULL AND tick_age_min IS NULL
 AND tick_change_1h IS NULL AND tick_volume_1h IS NULL AND tick_txn_1h IS NULL
 AND top10_holders_pct IS NULL AND buy_count_24h IS NULL AND sell_count_24h IS NULL
 AND buy_sell_ratio_24h IS NULL AND unique_buys_24h IS NULL AND unique_sells_24h IS NULL
 AND tick_change_4h IS NULL AND tick_change_24h IS NULL AND tick_volume_4h IS NULL
 AND tick_txn_24h IS NULL AND volume_to_liquidity IS NULL AND liquidity_to_mcap IS NULL
 AND float_ratio IS NULL AND tick_rich_age_min IS NULL"""
q2 = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP} AND ({theirs})").fetchone()["c"]
print("THEIR TEST market (21-col AND) =", q2)

# ---- token_static: socials_count/has_twitter/has_cmc_id are ALWAYS non-NULL
# when a token_static row exists (they are sums/booleans, never None). So
# has_twitter IS NULL <=> no token_static row at or before entry_ts.
q3 = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP} AND has_twitter IS NULL").fetchone()["c"]
q3b = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP} AND socials_count IS NULL").fetchone()["c"]
q3c = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP} AND has_cmc_id IS NULL").fetchone()["c"]
print(f"\nMY TEST static: has_twitter IS NULL = {q3} ({100*q3/n:.2f}%)  socials_count NULL={q3b}  has_cmc_id NULL={q3c}")

stat18 = """token_age_h IS NULL AND launchpad_name IS NULL AND migrated IS NULL
 AND graduation_percent IS NULL AND is_scam IS NULL AND mintable IS NULL AND freezable IS NULL
 AND socials_count IS NULL AND has_twitter IS NULL AND creator_prior_tokens IS NULL
 AND decimals IS NULL AND name_len IS NULL AND name_non_ascii IS NULL
 AND exchanges_count IS NULL AND listed_on_exchange IS NULL AND has_cmc_id IS NULL
 AND description_len IS NULL AND has_banner IS NULL"""
q4 = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP} AND ({stat18})").fetchone()["c"]
print("THEIR-EQUIV static (18-col AND) =", q4)

# ---- adversarial: do the market-NULL rows still carry price at t0 from the event?
q5 = con.execute(f"""SELECT COUNT(*) c,
   SUM(CASE WHEN price_usd IS NOT NULL THEN 1 ELSE 0 END) has_price,
   SUM(CASE WHEN market_cap IS NOT NULL THEN 1 ELSE 0 END) has_mcap,
   SUM(CASE WHEN total_volume IS NOT NULL THEN 1 ELSE 0 END) has_vol,
   SUM(CASE WHEN size_usd IS NOT NULL THEN 1 ELSE 0 END) has_size
   FROM training_rows WHERE {POP} AND tick_age_min IS NULL""").fetchone()
print("\nmarket-NULL rows: n=%d has price_usd=%d has market_cap=%d has total_volume=%d has size_usd=%d"
      % (q5["c"], q5["has_price"], q5["has_mcap"], q5["has_vol"], q5["has_size"]))

# do they have bars-derived price history / onchain / labels?
q6 = con.execute(f"""SELECT
   SUM(CASE WHEN bars_count_24h IS NOT NULL THEN 1 ELSE 0 END) bars,
   SUM(CASE WHEN ret_24h_before IS NOT NULL THEN 1 ELSE 0 END) ret24,
   SUM(CASE WHEN onchain_top10_pct IS NOT NULL THEN 1 ELSE 0 END) onchain,
   SUM(CASE WHEN final_return_48h IS NOT NULL THEN 1 ELSE 0 END) lbl
   FROM training_rows WHERE {POP} AND tick_age_min IS NULL""").fetchone()
print("market-NULL rows: bars_count_24h=%s ret_24h_before=%s onchain_top10=%s final_return_48h=%s"
      % (q6["bars"], q6["ret24"], q6["onchain"], q6["lbl"]))

con.close()
