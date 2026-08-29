import os, sqlite3, config
import features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

tr = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
for c in ("entry_lag_s", "candles_48h", "suspect_bars", "status", "entry_px"):
    print(f"training_rows has {c:14s}: {str(c in tr):5s}  in features.ROW_COLUMNS: {c in features.ROW_COLUMNS}", flush=True)

print("\nfresh outcomes totals:", flush=True)
for r in con.execute("SELECT status, COUNT(*) n FROM outcomes GROUP BY status ORDER BY n DESC"):
    print(" ", dict(r), flush=True)
print(" total:", con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], flush=True)

print("\nwatch entry_lag_s recheck:", flush=True)
print(dict(con.execute("""
  SELECT COUNT(*) n, SUM(entry_lag_s=0) z, SUM(entry_lag_s>0) p, SUM(entry_lag_s IS NULL) nul
    FROM outcomes WHERE kind='watch'""").fetchone()), flush=True)

# cheap proxy for "did this watch have an admission price": entry_px must equal
# the admission price when one existed, and admission_source is stamped.
print("\nwatch_windows admission price coverage:", flush=True)
print(dict(con.execute("""
  SELECT COUNT(*) n, SUM(admission_price_usd IS NOT NULL) with_px,
         SUM(admission_price_usd IS NULL) without_px
    FROM watch_windows""").fetchone()), flush=True)

con.close()
