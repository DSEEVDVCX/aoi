import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = con.execute

MODEL = """
  r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok'
  AND r.is_independent=1
  AND r.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
"""

print("feature_version =", q("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

n = q(f"SELECT COUNT(*) FROM training_rows r WHERE {MODEL}").fetchone()[0]
print("model population (base table, no dedup):", n)

# 1. do all model rows have an outcome row? use EXISTS not JOIN (different shape)
miss = q(f"""SELECT COUNT(*) FROM training_rows r WHERE {MODEL}
  AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind=r.kind AND o.key=r.key)""").fetchone()[0]
print("model rows with NO outcomes row:", miss)

# 2. my own truncation measure: recompute the flag from last_bar_lag_h, do not trust bars_truncated
row = q(f"""SELECT
    COUNT(*) tot,
    SUM(CASE WHEN o.last_bar_lag_h > 1.0 THEN 1 ELSE 0 END) lag_gt1,
    SUM(CASE WHEN o.bars_truncated = 1 THEN 1 ELSE 0 END) flag1,
    SUM(CASE WHEN o.bars_truncated = 1 AND NOT (o.last_bar_lag_h > 1.0) THEN 1 ELSE 0 END) disagree_a,
    SUM(CASE WHEN o.last_bar_lag_h > 1.0 AND o.bars_truncated <> 1 THEN 1 ELSE 0 END) disagree_b,
    SUM(CASE WHEN o.last_bar_lag_h IS NULL THEN 1 ELSE 0 END) lag_null,
    AVG(o.last_bar_lag_h) mean_lag, MAX(o.last_bar_lag_h) max_lag, MIN(o.last_bar_lag_h) min_lag
  FROM training_rows r, outcomes o
  WHERE o.kind=r.kind AND o.key=r.key AND {MODEL}""").fetchone()
print("\n-- recomputed vs stored flag --")
for k in row.keys():
    print(f"  {k} = {row[k]}")
print("  pct lag>1h = %.2f%%" % (100.0*row["lag_gt1"]/row["tot"]))

# 3. my own buckets (different boundaries than theirs)
print("\n-- my buckets on last_bar_lag_h --")
for r2 in q(f"""SELECT CASE
      WHEN o.last_bar_lag_h IS NULL THEN '0 null'
      WHEN o.last_bar_lag_h <= 1 THEN '1 <=1h'
      WHEN o.last_bar_lag_h <= 4 THEN '2 1-4h'
      WHEN o.last_bar_lag_h <= 12 THEN '3 4-12h'
      WHEN o.last_bar_lag_h <= 24 THEN '4 12-24h'
      WHEN o.last_bar_lag_h <= 44 THEN '5 24-44h'
      ELSE '6 >44h' END b, COUNT(*) n,
      ROUND(AVG(o.candles_48h),1) avg_candles,
      ROUND(AVG(o.final_return_48h),4) avg_final_ret,
      ROUND(AVG(o.is_rug)*100,1) pct_rug,
      ROUND(AVG(o.max_gain_48h),4) avg_maxgain48
    FROM training_rows r, outcomes o
    WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} GROUP BY 1 ORDER BY 1"""):
    print("  ", dict(r2))

# 4. candles_48h distribution
print("\n-- candles_48h --")
for r3 in q(f"""SELECT CASE
      WHEN o.candles_48h IS NULL THEN 'null'
      WHEN o.candles_48h < 50 THEN 'a <50'
      WHEN o.candles_48h < 200 THEN 'b 50-199'
      WHEN o.candles_48h < 500 THEN 'c 200-499'
      ELSE 'd >=500' END b, COUNT(*) n, MIN(o.candles_48h), MAX(o.candles_48h)
    FROM training_rows r, outcomes o
    WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} GROUP BY 1 ORDER BY 1"""):
    print("  ", tuple(r3))

# 5. PRAGMA: is any of the three actually a column?
cols = [c[1] for c in q("PRAGMA table_info(training_rows)").fetchall()]
print("\ntraining_rows column count:", len(cols))
for name in ("bars_truncated", "last_bar_lag_h", "candles_48h", "suspect_bars",
             "entry_px", "entry_lag_s", "final_return_48h"):
    print(f"  {name!r} in training_rows: {name in cols}")

con.close()
