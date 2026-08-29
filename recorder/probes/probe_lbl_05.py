import os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()
def show(t, rows):
    print("\n== " + t)
    for r in rows: print("   ", dict(r))
FV = "feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND " + FV)

# ---- 1. model-eligible in every respect EXCEPT feature_version
show("model-eligible but stale feature_version, by fv x network", q(
  "SELECT feature_version, COALESCE(network_id,'') net, COUNT(*) n FROM training_rows "
  "WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1 "
  "GROUP BY 1,2 ORDER BY 1, n DESC"))
print("\nTOTAL model-eligible-except-fv rows at fv!=12 =", q(
  "SELECT COUNT(*) c FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme' "
  "AND status='ok' AND is_independent=1 AND NOT (" + FV + ")")[0]["c"])

# ---- 2. labelled outcomes that would be model candidates but have NO training row
show("outcomes signal/ok/live/independent with NO training row, by network", q(
  "SELECT COALESCE(o.network_id,'') net, COUNT(*) n FROM outcomes o "
  "WHERE o.kind='signal' AND o.status='ok' AND o.entry_ts >= ? AND o.is_independent=1 "
  "AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key) "
  "GROUP BY 1 ORDER BY n DESC", (config.LIVE_START_TS,)))
show("outcomes signal/ok/live/independent with NO *current-fv* training row, by network", q(
  "SELECT COALESCE(o.network_id,'') net, COUNT(*) n FROM outcomes o "
  "WHERE o.kind='signal' AND o.status='ok' AND o.entry_ts >= ? AND o.is_independent=1 "
  "AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key AND r."
  + FV + ") GROUP BY 1 ORDER BY n DESC", (config.LIVE_START_TS,)))

# ---- 3. LABEL HORIZON TRUTH: is final_return_48h really measured at 48h?
show("MODEL: bars_truncated (from outcomes)", q(
  f"SELECT o.bars_truncated, COUNT(*) n FROM training_rows r JOIN outcomes o "
  f"ON o.kind=r.kind AND o.key=r.key WHERE r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' "
  f"AND r.status='ok' AND r.is_independent=1 AND r.{FV} GROUP BY 1 ORDER BY n DESC"))
show("MODEL: last_bar_lag_h buckets (hours the series ended BEFORE the 48h mark)", q(
  f"SELECT CASE WHEN o.last_bar_lag_h IS NULL THEN 'NULL' WHEN o.last_bar_lag_h<=1 THEN '<=1h (full window)'"
  " WHEN o.last_bar_lag_h<=6 THEN '1-6h short' WHEN o.last_bar_lag_h<=24 THEN '6-24h short'"
  " WHEN o.last_bar_lag_h<=40 THEN '24-40h short' ELSE '>40h short' END b, COUNT(*) n "
  f"FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key "
  f"WHERE r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok' "
  f"AND r.is_independent=1 AND r.{FV} GROUP BY 1 ORDER BY n DESC"))
show("MODEL: candles_48h buckets (5m bars; a full 48h window = ~576)", q(
  f"SELECT CASE WHEN o.candles_48h IS NULL THEN 'NULL' WHEN o.candles_48h=0 THEN '0'"
  " WHEN o.candles_48h<50 THEN '1-49' WHEN o.candles_48h<200 THEN '50-199'"
  " WHEN o.candles_48h<400 THEN '200-399' WHEN o.candles_48h<550 THEN '400-549' ELSE '550+' END b,"
  f" COUNT(*) n FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key "
  f"WHERE r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok' "
  f"AND r.is_independent=1 AND r.{FV} GROUP BY 1 ORDER BY n DESC"))
show("MODEL: median/percentiles of last_bar_lag_h", q(
  f"SELECT MIN(o.last_bar_lag_h) mn, MAX(o.last_bar_lag_h) mx, AVG(o.last_bar_lag_h) avg "
  f"FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key "
  f"WHERE r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok' "
  f"AND r.is_independent=1 AND r.{FV}"))
print("\nMODEL rows with bars_truncated=1 (label horizon < 47h) =", q(
  f"SELECT COUNT(*) c FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key "
  f"WHERE r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok' "
  f"AND r.is_independent=1 AND r.{FV} AND o.bars_truncated=1")[0]["c"])
print("bars_truncated present in training_rows columns? ->",
      "bars_truncated" in [r["name"] for r in q("PRAGMA table_info(training_rows)")])
print("last_bar_lag_h present in training_rows columns? ->",
      "last_bar_lag_h" in [r["name"] for r in q("PRAGMA table_info(training_rows)")])
print("candles_48h present in training_rows columns? ->",
      "candles_48h" in [r["name"] for r in q("PRAGMA table_info(training_rows)")])

# ---- 4. entry_lag_s / entry_px sanity for MODEL
show("MODEL: entry_lag_s buckets", q(
  f"SELECT CASE WHEN o.entry_lag_s IS NULL THEN 'NULL' WHEN o.entry_lag_s=0 THEN '0'"
  " WHEN o.entry_lag_s<=300 THEN '1-300s' WHEN o.entry_lag_s<=900 THEN '301-900s' ELSE '901-1800s' END b,"
  f" COUNT(*) n FROM training_rows r JOIN outcomes o ON o.kind=r.kind AND o.key=r.key "
  f"WHERE r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok' "
  f"AND r.is_independent=1 AND r.{FV} GROUP BY 1 ORDER BY n DESC"))

# ---- 5. time_to_peak_h = 48.0 pile-up and max_drawdown=0 rows
print("\nMODEL time_to_peak_h = 48.0 exactly =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND time_to_peak_h=48.0")[0]["c"])
print("MODEL time_to_peak_h >= 47.9 =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND time_to_peak_h>=47.9")[0]["c"])
show("MODEL max_drawdown_48h=0 rows: candles_48h", q(
  f"SELECT o.candles_48h, o.suspect_bars, COUNT(*) n FROM training_rows r JOIN outcomes o "
  f"ON o.kind=r.kind AND o.key=r.key WHERE r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' "
  f"AND r.status='ok' AND r.is_independent=1 AND r.{FV} AND r.max_drawdown_48h=0 "
  "GROUP BY 1,2 ORDER BY n DESC LIMIT 12"))
print("MODEL max_gain_48h=0 exactly =", q(
  f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL} AND max_gain_48h=0")[0]["c"])

# ---- 6. duplicate labels for the same economic event (kind=signal vs kind=watch same token/ts)
print("\noutcomes: distinct (token,entry_ts) with BOTH a signal and a watch row =", q(
  "SELECT COUNT(*) c FROM (SELECT token_address, entry_ts FROM outcomes WHERE kind='signal' "
  "INTERSECT SELECT token_address, entry_ts FROM outcomes WHERE kind='watch')")[0]["c"])
con.close()
