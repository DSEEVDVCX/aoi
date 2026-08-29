import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
W = config.LABEL_WINDOW_HOURS * 3600
MAXLAG = config.LABEL_ENTRY_MAX_LAG_SECONDS

# Harden the "bars arrived after labeling" test against INSERT OR REPLACE
# refreshing fetched_at: ask whether the token had ANY bar row at all whose
# fetched_at predates labeled_at, and how spread the window's fetch stamps are.
sql = """
SELECT o.kind, o.key, o.entry_ts, o.labeled_at,
  (SELECT COUNT(*) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS bars_now,
  (SELECT MIN(b.fetched_at) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
  ) AS token_first_fetch_any_ts,
  (SELECT COUNT(DISTINCT b.fetched_at) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
  ) AS distinct_fetch_stamps,
  (SELECT MIN(b.ts) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>=o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL
       AND b.c_suspect=0) AS first_ok_close_ts
 FROM outcomes o
WHERE o.status='no_entry'
"""
rows = [dict(r) for r in con.execute(sql, (W, W, W)).fetchall()]
have = [r for r in rows if r["bars_now"] > 0]
print("no_entry rows with window bars present now:", len(have))

never_before = [r for r in have
                if r["token_first_fetch_any_ts"] and r["token_first_fetch_any_ts"] > r["labeled_at"]]
print("  token had NO bar row of ANY ts predating labeled_at:", len(never_before))
some_before = [r for r in have if r not in never_before]
print("  token had at least one bar predating labeled_at    :", len(some_before))
print("  of those, entry bar LATE (>1800s) -> genuine no_entry:",
      sum(1 for r in some_before
          if r["first_ok_close_ts"] is not None
          and r["first_ok_close_ts"] - r["entry_ts"] > MAXLAG))
print("  of those, entry bar within 1800s (would have been ok):",
      sum(1 for r in some_before
          if r["first_ok_close_ts"] is not None
          and r["first_ok_close_ts"] - r["entry_ts"] <= MAXLAG))
print("  distinct fetched_at stamps in window, sample:",
      [r["distinct_fetch_stamps"] for r in have[:15]])

print()
print("== is candles_48h/suspect_bars used by any consumer on non-ok rows? ==")
for r in con.execute("""
  SELECT status, COUNT(*) n FROM outcomes
   WHERE analysis_eligible=1 GROUP BY status"""):
    print("analysis_eligible=1:", dict(r))
for r in con.execute("""
  SELECT status, COUNT(*) n, SUM(candles_48h=0) c0 FROM outcomes
   WHERE kind='watch' AND design_version>=3 GROUP BY status"""):
    print("watch dv>=3:", dict(r))

print()
print("== no_bars rows: is candles_48h=0 true there? (definitional) ==")
r = con.execute("""
  SELECT COUNT(*) n,
    SUM((SELECT COUNT(*) FROM token_bars b
          WHERE b.token_address=o.token_address
            AND b.network_id=COALESCE(o.network_id,'')
            AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
            AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL)>0) with_bars_now
   FROM outcomes o WHERE o.status='no_bars'""", (W,)).fetchone()
print(dict(r))

con.close()
