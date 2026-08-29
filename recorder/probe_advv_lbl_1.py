import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

print("== A. my own shape: no_entry rows, candles_48h distribution ==")
for r in q("""
  SELECT status,
         COUNT(*) AS n,
         SUM(candles_48h IS NULL) AS c_null,
         SUM(candles_48h = 0)     AS c_zero,
         SUM(candles_48h > 0)     AS c_pos,
         SUM(suspect_bars IS NULL) AS s_null,
         SUM(suspect_bars = 0)     AS s_zero,
         SUM(suspect_bars > 0)     AS s_pos
    FROM outcomes GROUP BY status ORDER BY n DESC"""):
    print(dict(r))

print()
print("== B. total outcomes + by kind/status ==")
for r in q("SELECT kind, status, COUNT(*) n FROM outcomes GROUP BY kind, status ORDER BY kind, n DESC"):
    print(dict(r))

print()
print("== C. entry_lag_s by kind (my own phrasing) ==")
for r in q("""
  SELECT kind, COUNT(*) n,
         SUM(entry_lag_s IS NULL) AS lag_null,
         SUM(entry_lag_s = 0)     AS lag_zero,
         SUM(entry_lag_s > 0)     AS lag_pos,
         MIN(entry_lag_s) mn, MAX(entry_lag_s) mx
    FROM outcomes GROUP BY kind"""):
    print(dict(r))

print()
print("== D. watch rows: entry_lag_s=0 vs admission price present ==")
for r in q("""
  SELECT (w.admission_price_usd IS NOT NULL) AS has_adm,
         COUNT(*) n,
         SUM(o.entry_lag_s = 0) lag_zero,
         SUM(o.entry_lag_s > 0) lag_pos,
         SUM(o.entry_lag_s IS NULL) lag_null
    FROM outcomes o
    JOIN watchlist w
      ON w.token_address || ':' || w.network_id || ':' || w.first_seen_at = o.key
   WHERE o.kind='watch'
   GROUP BY has_adm"""):
    print(dict(r))

con.close()
