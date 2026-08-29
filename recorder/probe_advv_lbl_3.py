import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
W = config.LABEL_WINDOW_HOURS * 3600
MAXLAG = config.LABEL_ENTRY_MAX_LAG_SECONDS

sql = """
SELECT o.kind, o.key, o.entry_ts, o.labeled_at, o.entry_px, o.suspect_bars,
  (SELECT COUNT(*) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS bars_now,
  (SELECT MIN(b.fetched_at) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS first_fetched_at,
  (SELECT MIN(b.ts) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>=o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL
       AND b.c_suspect=0) AS first_ok_close_ts,
  (SELECT b.c FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>=o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL
       AND b.c_suspect=0 ORDER BY b.ts LIMIT 1) AS first_ok_close_px
 FROM outcomes o
WHERE o.status='no_entry'
"""
rows = [dict(r) for r in con.execute(sql, (W, W, W, W)).fetchall()]
have = [r for r in rows if r["bars_now"] > 0]
print("no_entry rows with bars present NOW:", len(have), "of", len(rows))

after = sum(1 for r in have if r["first_fetched_at"] and r["first_fetched_at"] > r["labeled_at"])
before = len(have) - after
print("  earliest bar fetched_at > labeled_at (bars arrived AFTER labeling):", after)
print("  bars already present at labeling time                            :", before)

print()
print("of those present-at-label rows, why no_entry?")
late = [r for r in have
        if not (r["first_fetched_at"] and r["first_fetched_at"] > r["labeled_at"])
        and r["first_ok_close_ts"] is not None
        and r["first_ok_close_ts"] - r["entry_ts"] > MAXLAG]
intime = [r for r in have
          if not (r["first_fetched_at"] and r["first_fetched_at"] > r["labeled_at"])
          and r["first_ok_close_ts"] is not None
          and r["first_ok_close_ts"] - r["entry_ts"] <= MAXLAG]
noclose = [r for r in have
           if not (r["first_fetched_at"] and r["first_fetched_at"] > r["labeled_at"])
           and r["first_ok_close_ts"] is None]
print("  entry bar LATE (>1800s) -> genuine no_entry :", len(late))
print("  entry bar in time, entry_px would be ok?    :", len(intime))
print("  no non-suspect close                        :", len(noclose))
print("  in-time ones, close px values (first 10):",
      [r["first_ok_close_px"] for r in intime[:10]])

print()
print("== sanity: does status='ok' ever get candles_48h=0? ==")
print(dict(con.execute(
    "SELECT COUNT(*) n, SUM(candles_48h=0) z FROM outcomes WHERE status='ok'").fetchone()))

print()
print("== does any no_entry / no_bars row reach the MODEL POPULATION? ==")
fv = con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
).fetchone()[0]
print("current_feature_version =", fv)
print(dict(con.execute("""
  SELECT COUNT(*) model_rows,
         SUM(status<>'ok') non_ok,
         SUM(suspect_bars IS NULL) sb_null,
         SUM(suspect_bars=0) sb_zero
    FROM training_rows
   WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
     AND is_independent=1 AND feature_version=?""", (fv,)).fetchone()))

print()
print("== do no_entry rows exist in training_rows at all? ==")
for r in con.execute("""
  SELECT status, COUNT(*) n FROM training_rows
   WHERE kind='signal' AND is_live=1 AND feature_version=?
   GROUP BY status ORDER BY n DESC""", (fv,)):
    print(dict(r))

con.close()
