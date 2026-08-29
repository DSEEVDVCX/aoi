import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
W = config.LABEL_WINDOW_HOURS * 3600
MAXLAG = config.LABEL_ENTRY_MAX_LAG_SECONDS

sql = """
SELECT o.key, o.kind, o.entry_ts, o.labeled_at,
  (SELECT COUNT(*) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS bars_now,
  (SELECT MIN(b.fetched_at) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS win_min_fetch,
  (SELECT MIN(b.ts) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>=o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL
       AND b.c_suspect=0) AS first_ok_ts,
  (SELECT b.c FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>=o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL
       AND b.c_suspect=0 ORDER BY b.ts LIMIT 1) AS first_ok_c
 FROM outcomes o WHERE o.status='no_entry'
"""
rows = [dict(r) for r in con.execute(sql, (W, W, W, W)).fetchall()]
have = [r for r in rows if r["bars_now"] > 0]

intime = [r for r in have
          if r["first_ok_ts"] is not None and r["first_ok_ts"] - r["entry_ts"] <= MAXLAG]
late = [r for r in have
        if r["first_ok_ts"] is not None and r["first_ok_ts"] - r["entry_ts"] > MAXLAG]
print("no_entry with window bars now :", len(have))
print("  entry bar in time (<=1800s)  :", len(intime))
print("  entry bar late   (> 1800s)   :", len(late))

it_after = sum(1 for r in intime if r["win_min_fetch"] and r["win_min_fetch"] > r["labeled_at"])
print("  of the in-time ones, window bars first fetched AFTER labeled_at:",
      it_after, "of", len(intime))
bad = [r for r in intime if not (r["win_min_fetch"] and r["win_min_fetch"] > r["labeled_at"])]
print("  UNEXPLAINED in-time rows (bars predate label, yet no_entry):", len(bad))
for r in bad[:6]:
    print("    ", r["kind"], r["key"], "labeled", r["labeled_at"],
          "win_min_fetch", r["win_min_fetch"], "lag_s", r["first_ok_ts"] - r["entry_ts"],
          "close", r["first_ok_c"])
print("  in-time close prices <=0 or non-finite:",
      sum(1 for r in intime if r["first_ok_c"] is None or r["first_ok_c"] <= 0))

print()
print("== TRUTHFULNESS TALLY for candles_48h=0 on the 1,568 no_entry rows ==")
zero_now = len(rows) - len(have)
lateN = len(late)
after = sum(1 for r in have if r["win_min_fetch"] and r["win_min_fetch"] > r["labeled_at"])
print("  window empty even today (0 is simply true) :", zero_now)
print("  window bars all first written after label  :", after)
print("  entry bar late -> no_entry is correct      :", lateN)
print("  residual rows where 0 is arguably wrong    :", len(bad))

con.close()
