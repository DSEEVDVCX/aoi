import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
for r in con.execute("""
SELECT CASE WHEN wl.first_seen_at IS NULL THEN 'no watchlist row'
            WHEN w.first_seen_at > wl.first_seen_at THEN 'opened mid-watch'
            ELSE 'opened with watchlist entry' END c,
       COUNT(*) n, ROUND(AVG(o.candles_48h),1) mean_candles,
       ROUND(AVG(o.bars_truncated),3) frac_truncated,
       ROUND(AVG(o.last_bar_lag_h),2) mean_last_bar_lag_h,
       ROUND(AVG(o.final_return_48h),4) mean_ret
  FROM watch_windows w
  JOIN outcomes o ON o.kind='watch' AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at
  LEFT JOIN watchlist wl ON wl.token_address=w.token_address AND wl.network_id=w.network_id
 WHERE w.design_version>=3 AND w.is_control=0 AND w.admission_source='trending' AND o.status='ok'
 GROUP BY 1 ORDER BY n DESC"""):
    print(dict(r))
con.close()
