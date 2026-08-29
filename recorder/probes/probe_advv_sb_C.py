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

show("training_rows label completeness by bucket", q(f"""
  SELECT CASE WHEN suspect_bars IS NULL THEN 'a_NULL' WHEN suspect_bars=0 THEN 'b_zero' ELSE 'c_pos' END bucket,
         COUNT(*) n,
         SUM(final_return_48h IS NULL) fr_null,
         SUM(max_gain_48h IS NULL) mg48_null,
         SUM(max_gain_24h IS NULL) mg24_null,
         SUM(is_rug IS NULL) rug_null,
         SUM(time_to_peak_h IS NULL) ttp_null
    FROM training_rows WHERE {MODEL} GROUP BY bucket ORDER BY bucket"""))

show("label extremes by bucket", q(f"""
  SELECT CASE WHEN suspect_bars IS NULL THEN 'a_NULL' WHEN suspect_bars=0 THEN 'b_zero' ELSE 'c_pos' END bucket,
         COUNT(*) n,
         ROUND(MAX(max_gain_48h),3) mx_gain48,
         SUM(max_gain_48h > 10) gt_1000pct,
         SUM(max_gain_48h > 100) gt_10000pct,
         ROUND(MIN(max_drawdown_48h),4) worst_dd,
         ROUND(MAX(final_return_48h),3) mx_final,
         ROUND(AVG(final_return_48h),4) avg_final,
         ROUND(AVG(is_rug),4) rug_rate,
         ROUND(AVG(CASE WHEN max_gain_48h>=0.20 THEN 1.0 ELSE 0.0 END),4) up20_rate
    FROM training_rows WHERE {MODEL} GROUP BY bucket ORDER BY bucket"""))

# --- outcomes-side: the legacy rows' own quality columns
show("outcomes NULL-suspect rows: other quality cols", q("""
  SELECT COUNT(*) n,
         SUM(candles_48h IS NULL) cd_null, SUM(candles_48h=0) cd_zero,
         SUM(last_bar_lag_h IS NULL) lag_null,
         SUM(bars_truncated IS NULL) trunc_null,
         SUM(bars_truncated=1) trunc_one,
         SUM(entry_lag_s IS NULL) elag_null
    FROM outcomes WHERE suspect_bars IS NULL"""))
show("outcomes non-NULL-suspect rows: same cols", q("""
  SELECT COUNT(*) n,
         SUM(candles_48h IS NULL) cd_null, SUM(candles_48h=0) cd_zero,
         SUM(last_bar_lag_h IS NULL) lag_null,
         SUM(bars_truncated IS NULL) trunc_null,
         SUM(bars_truncated=1) trunc_one,
         SUM(entry_lag_s IS NULL) elag_null
    FROM outcomes WHERE suspect_bars IS NOT NULL"""))
con.close()
