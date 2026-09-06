import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

def show(t, rows):
    print("==", t)
    for r in rows:
        print("   ", dict(r))
    print()

FV = 12
MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND feature_version=%d" % FV)

cols = [r["name"] for r in q("PRAGMA table_info(training_rows)")]
print("label-ish training_rows cols:",
      [c for c in cols if any(k in c for k in ("gain", "return", "rug", "peak", "draw", "candle", "suspect", "px", "lag", "trunc"))])
print()

# --- outcomes side: eligibility bookkeeping of the legacy (NULL) outcomes
show("outcomes by (suspect_bars NULL?, analysis_eligible, exclusion_reason)", q("""
  SELECT CASE WHEN suspect_bars IS NULL THEN 'NULL' ELSE 'set' END g,
         analysis_eligible, exclusion_reason, design_version, COUNT(*) n
    FROM outcomes GROUP BY g, analysis_eligible, exclusion_reason, design_version
   ORDER BY g, n DESC"""))

# --- do the 711 rows carry usable labels?
show("NULL bucket label completeness", q(f"""
  SELECT COUNT(*) n,
         SUM(final_return_48h IS NULL) fr_null,
         SUM(max_gain_48h IS NULL) mg48_null,
         SUM(max_gain_24h IS NULL) mg24_null,
         SUM(is_rug IS NULL) rug_null,
         SUM(candles_48h IS NULL) cd_null,
         SUM(last_bar_lag_h IS NULL) lag_null,
         SUM(bars_truncated IS NULL) trunc_null
    FROM training_rows WHERE {MODEL} AND suspect_bars IS NULL"""))

show("ZERO bucket same completeness (control)", q(f"""
  SELECT COUNT(*) n,
         SUM(final_return_48h IS NULL) fr_null,
         SUM(max_gain_48h IS NULL) mg48_null,
         SUM(is_rug IS NULL) rug_null,
         SUM(last_bar_lag_h IS NULL) lag_null,
         SUM(bars_truncated IS NULL) trunc_null
    FROM training_rows WHERE {MODEL} AND suspect_bars = 0"""))

# --- label magnitude: pre-flag era vs post-flag era
show("label extremes by bucket", q(f"""
  SELECT CASE WHEN suspect_bars IS NULL THEN 'a_NULL' WHEN suspect_bars=0 THEN 'b_zero' ELSE 'c_pos' END bucket,
         COUNT(*) n,
         ROUND(MAX(max_gain_48h),3) mx_gain48,
         SUM(max_gain_48h > 10) gt_1000pct,
         SUM(max_gain_48h > 100) gt_10000pct,
         ROUND(MIN(max_drawdown_48h),4) worst_dd,
         ROUND(MAX(final_return_48h),3) mx_final,
         ROUND(AVG(final_return_48h),4) avg_final,
         ROUND(AVG(is_rug),4) rug_rate
    FROM training_rows WHERE {MODEL} GROUP BY bucket ORDER BY bucket"""))
con.close()
