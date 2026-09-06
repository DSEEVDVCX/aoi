import os, sqlite3, config
import features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

tr = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
for c in ("entry_lag_s", "candles_48h", "suspect_bars", "status"):
    print(f"training_rows has {c:14s}: {c in tr}   in features.ROW_COLUMNS: {c in features.ROW_COLUMNS}")

print()
print("fresh outcomes totals:")
for r in con.execute("SELECT status, COUNT(*) n FROM outcomes GROUP BY status ORDER BY n DESC"):
    print(" ", dict(r))
print(" total:", con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])

print()
print("watch entry_lag_s recheck:")
print(dict(con.execute("""
  SELECT COUNT(*) n, SUM(entry_lag_s=0) z, SUM(entry_lag_s>0) p, SUM(entry_lag_s IS NULL) nul
    FROM outcomes WHERE kind='watch'""").fetchone()))

print()
print("watch rows joined to watch_windows admission price:")
for r in con.execute("""
  SELECT (ww.admission_price_usd IS NOT NULL) has_adm, COUNT(*) n,
         SUM(o.entry_lag_s=0) z, SUM(o.entry_lag_s>0) p, SUM(o.entry_lag_s IS NULL) nul
    FROM outcomes o
    LEFT JOIN watch_windows ww
      ON ww.token_address||':'||ww.network_id||':'||ww.first_seen_at = o.key
   WHERE o.kind='watch'
   GROUP BY has_adm"""):
    print(" ", dict(r))

con.close()
