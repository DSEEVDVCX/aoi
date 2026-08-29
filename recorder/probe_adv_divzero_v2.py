import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = """
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta
                          WHERE key='current_feature_version'),'0') AS INTEGER)
"""

print("== the 'buy>0 / sell=0' subset: is the signal lost or carried elsewhere? ==")
r = con.execute("""
SELECT COUNT(*) n,
       SUM(CASE WHEN flow_net_volume_5m IS NOT NULL THEN 1 ELSE 0 END) net_present,
       SUM(CASE WHEN flow_buy_volume_5m IS NOT NULL THEN 1 ELSE 0 END) buy_present,
       SUM(CASE WHEN flow_sell_volume_5m=0 THEN 1 ELSE 0 END) sell_zero_present,
       SUM(CASE WHEN flow_age_min IS NOT NULL THEN 1 ELSE 0 END) age_present,
       SUM(CASE WHEN flow_buy_sell_volume_ratio_1h IS NOT NULL THEN 1 ELSE 0 END) ratio1h_ok,
       SUM(CASE WHEN flow_buy_sell_volume_ratio_24h IS NOT NULL THEN 1 ELSE 0 END) ratio24h_ok
""" + POP + " AND flow_buy_sell_volume_ratio_5m IS NULL AND flow_sell_volume_5m=0 AND flow_buy_volume_5m>0").fetchone()
print(dict(r))

print()
print("== 'no flow at all' bucket for contrast ==")
r = con.execute("""
SELECT COUNT(*) n,
       SUM(CASE WHEN flow_buy_volume_5m IS NULL AND flow_sell_volume_5m IS NULL
                 AND flow_net_volume_5m IS NULL THEN 1 ELSE 0 END) all_raw_null
""" + POP + " AND flow_age_min IS NULL").fetchone()
print(dict(r))

print()
print("== longer-horizon ratios: does a zero denominator ever hit 1h/24h? ==")
cols = {c[1] for c in con.execute("PRAGMA table_info(training_rows)").fetchall()}
print("flow_sell_volume_1h in training_rows:", "flow_sell_volume_1h" in cols)
print("flow_sell_volume_24h in training_rows:", "flow_sell_volume_24h" in cols)
r = con.execute("""
SELECT SUM(CASE WHEN flow_buy_sell_volume_ratio_1h IS NULL AND flow_net_volume_1h IS NOT NULL
                THEN 1 ELSE 0 END) r1h_null_but_net,
       SUM(CASE WHEN flow_buy_sell_volume_ratio_24h IS NULL AND flow_net_volume_24h IS NOT NULL
                THEN 1 ELSE 0 END) r24h_null_but_net
""" + POP + " AND flow_age_min IS NOT NULL").fetchone()
print(dict(r))

print()
print("== THEIR EXACT SQL, re-run verbatim ==")
r = con.execute("""
SELECT SUM(CASE WHEN flow_buy_sell_volume_ratio_5m IS NULL AND flow_buy_volume_5m IS NOT NULL AND flow_sell_volume_5m=0 THEN 1 ELSE 0 END) vol5m,
       SUM(CASE WHEN flow_buy_sell_count_ratio_5m IS NULL AND flow_buy_count_5m IS NOT NULL AND flow_sell_count_5m=0 THEN 1 ELSE 0 END) cnt5m,
       SUM(CASE WHEN flow_unique_ratio_5m IS NULL AND flow_unique_buys_5m IS NOT NULL AND flow_buy_count_5m=0 THEN 1 ELSE 0 END) uniq5m,
       SUM(CASE WHEN flow_trade_size_5m IS NULL AND flow_buy_volume_5m IS NOT NULL AND flow_buy_count_5m=0 THEN 1 ELSE 0 END) size5m,
       SUM(CASE WHEN social_holder_ratio IS NULL AND social_holder_authors IS NOT NULL AND social_thesis_authors=0 THEN 1 ELSE 0 END) shr,
       SUM(CASE WHEN flow_age_min IS NOT NULL THEN 1 ELSE 0 END) has_flow, COUNT(*) n
  FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
   AND status='ok' AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
""").fetchone()
print(dict(r))

print()
print("== the single platform_underwater_ratio row: what does the SOURCE table say? ==")
row = con.execute("""
SELECT token_address, network_id, entry_ts, platform_holders, platform_underwater_ratio
""" + POP + " AND platform_underwater_ratio IS NULL AND platform_holders=0 LIMIT 1").fetchone()
print("training row:", dict(row) if row else None)
if row:
    src = con.execute("""
SELECT platform_holders, platform_holders_listed, platform_underwater, recorded_at, source
  FROM token_holders
 WHERE token_address=? AND network_id=? AND source='hodlers_top'
   AND CAST(strftime('%s', recorded_at) AS INTEGER) <= ?
 ORDER BY recorded_at DESC LIMIT 2""", (row["token_address"], row["network_id"], row["entry_ts"])).fetchall()
    for s in src:
        print("  source snapshot:", dict(s))

print()
print("== counterfactual: rows where platform_holders>0 but listed would be 0 ==")
r = con.execute("""
SELECT COUNT(*) n FROM token_holders
 WHERE source='hodlers_top' AND platform_holders_listed=0 AND platform_holders>0""").fetchone()
print("token_holders snapshots with listed=0 but platform_holders>0:", r["n"])
r = con.execute("""
SELECT COUNT(*) n FROM token_holders
 WHERE source='hodlers_top' AND platform_holders_listed=0""").fetchone()
print("token_holders snapshots with listed=0 at all:", r["n"])
con.close()
