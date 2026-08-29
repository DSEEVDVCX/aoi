import os, sys, sqlite3, config, time
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, args=()):
    t0 = time.time()
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:45]:
        print("   ", {k: (str(v).encode('ascii','replace').decode() if isinstance(v,str) else v) for k,v in dict(r).items()})
    if len(rows) > 45:
        print(f"    ... {len(rows)} rows total")
    return rows

q("the rows missing token_created_at", """
SELECT s.token_address, s.network_id, s.recorded_at,
       (SELECT COUNT(*) FROM watch_windows w WHERE w.token_address=s.token_address AND w.network_id=s.network_id) windows,
       (SELECT active FROM watchlist w WHERE w.token_address=s.token_address AND w.network_id=s.network_id) wl_active,
       (SELECT COUNT(*) FROM training_rows t WHERE t.token_address=s.token_address AND t.network_id=s.network_id) trows
  FROM token_static s WHERE s.token_created_at IS NULL ORDER BY s.recorded_at""")

q("token_static rows with no watchlist and no window", """
SELECT s.token_address, s.network_id, s.recorded_at FROM token_static s
 WHERE NOT EXISTS (SELECT 1 FROM watchlist w WHERE w.token_address=s.token_address AND w.network_id=s.network_id)
   AND NOT EXISTS (SELECT 1 FROM watch_windows x WHERE x.token_address=s.token_address AND x.network_id=s.network_id)""")

print()
print("=== unlabeled CLOSED watch_windows: why ===")
q("closed windows with no outcome, split by bars_fetch_state gate", """
SELECT CASE
         WHEN b.token_address IS NULL THEN 'no_bars_fetch_state_row'
         WHEN b.last_status='ok' AND b.last_fetch_at >= w.watch_until THEN 'gate_open_should_label'
         WHEN b.last_status='no_data' AND b.attempts>=3 THEN 'gate_open_should_label'
         WHEN b.last_status='ok' THEN 'ok_but_last_fetch_before_watch_until'
         ELSE 'other_status_'||COALESCE(b.last_status,'?')
       END reason, COUNT(*) n, MIN(w.first_seen_at) mn, MAX(w.first_seen_at) mx
  FROM watch_windows w
  LEFT JOIN bars_fetch_state b ON b.token_address=w.token_address AND b.network_id=w.network_id
 WHERE w.watch_until < strftime('%Y-%m-%dT%H:%M:%S','now')
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
                    AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
 GROUP BY 1 ORDER BY 2 DESC""")

q("closed unlabeled windows by network", """
SELECT w.network_id, COUNT(*) n, COUNT(DISTINCT w.token_address) coins
  FROM watch_windows w
 WHERE w.watch_until < strftime('%Y-%m-%dT%H:%M:%S','now')
   AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
                    AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
 GROUP BY 1 ORDER BY 2 DESC""")

q("ALL closed windows: labeled vs not, by network (denominator)", """
SELECT w.network_id, COUNT(*) closed_windows,
  SUM(CASE WHEN EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='watch'
        AND o.key=w.token_address||':'||w.network_id||':'||w.first_seen_at) THEN 1 ELSE 0 END) labeled
  FROM watch_windows w
 WHERE w.watch_until < strftime('%Y-%m-%dT%H:%M:%S','now')
 GROUP BY 1 ORDER BY 2 DESC""")

q("91 signal outcomes >1h from recorded_at: compare to feed ts", """
SELECT COUNT(*) n,
  SUM(CASE WHEN ABS(o.entry_ts - CAST(strftime('%s',s.ts) AS INTEGER)) <= 60 THEN 1 ELSE 0 END) matches_feed_ts,
  MIN(o.entry_ts - CAST(strftime('%s',s.recorded_at) AS INTEGER)) mn_delta,
  MAX(o.entry_ts - CAST(strftime('%s',s.recorded_at) AS INTEGER)) mx_delta
 FROM outcomes o JOIN signal_events s ON s.id=o.key
 WHERE o.kind='signal' AND ABS(o.entry_ts - CAST(strftime('%s',s.recorded_at) AS INTEGER)) > 3600""")

q("all signal outcomes: entry_ts vs feed ts mismatch", """
SELECT COUNT(*) total,
  SUM(CASE WHEN ABS(o.entry_ts - CAST(strftime('%s',s.ts) AS INTEGER)) > 60 THEN 1 ELSE 0 END) mismatch_gt60s
 FROM outcomes o JOIN signal_events s ON s.id=o.key WHERE o.kind='signal'""")

con.close()
