import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
W = config.LABEL_WINDOW_HOURS * 3600
MAXLAG = config.LABEL_ENTRY_MAX_LAG_SECONDS

# For every no_entry outcome, recount what the labeler actually had available.
sql = """
SELECT o.kind, o.key, o.entry_ts,
  (SELECT COUNT(*) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>=o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS usable_bars_in_range,
  (SELECT COUNT(*) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS bars_after_entry_ts,
  (SELECT MIN(b.ts) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5' AND b.ts>=o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL
       AND b.c_suspect=0) AS first_ok_close_ts,
  (SELECT COUNT(*) FROM token_bars b
     WHERE b.token_address=o.token_address AND b.network_id=COALESCE(o.network_id,'')
       AND b.resolution='5'
       AND (b.h_suspect=1 OR b.l_suspect=1 OR b.c_suspect=1)
       AND b.ts>o.entry_ts AND b.ts<=o.entry_ts+?
       AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL) AS suspect_after_entry
 FROM outcomes o
WHERE o.status='no_entry'
"""
rows = con.execute(sql, (W, W, W, W)).fetchall()
print("no_entry rows examined:", len(rows))

zero_bars = sum(1 for r in rows if r["usable_bars_in_range"] == 0)
no_ok_close = sum(1 for r in rows if r["first_ok_close_ts"] is None)
late_entry = sum(
    1 for r in rows
    if r["first_ok_close_ts"] is not None and r["first_ok_close_ts"] - r["entry_ts"] > MAXLAG
)
in_time_entry = sum(
    1 for r in rows
    if r["first_ok_close_ts"] is not None and r["first_ok_close_ts"] - r["entry_ts"] <= MAXLAG
)
would_be_pos = sum(1 for r in rows if r["bars_after_entry_ts"] > 0)
susp_pos = sum(1 for r in rows if r["suspect_after_entry"] > 0)

print("  usable bars in [entry, entry+48h] == 0 :", zero_bars)
print("  no non-suspect-close bar at all        :", no_ok_close)
print("  entry bar exists but LATE (>1800s)     :", late_entry)
print("  entry bar exists and IN TIME (px bad)  :", in_time_entry)
print("  bars strictly after entry_ts > 0       :", would_be_pos)
print("  suspect bars after entry_ts > 0        :", susp_pos)

print()
print("distribution of bars_after_entry_ts for no_entry rows:")
buckets = {}
for r in rows:
    n = r["bars_after_entry_ts"]
    b = "0" if n == 0 else "1-9" if n < 10 else "10-99" if n < 100 else "100+"
    buckets[b] = buckets.get(b, 0) + 1
print(buckets)

print()
print("sample of no_entry rows that DO have bars after entry_ts:")
shown = 0
for r in rows:
    if r["bars_after_entry_ts"] > 0:
        print(dict(r))
        shown += 1
        if shown >= 8:
            break

con.close()
