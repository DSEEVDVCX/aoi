import os, sqlite3, config, time

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
        print("   ", dict(r))
    if len(rows) > 45:
        print(f"    ... {len(rows)} rows total")
    return rows

print("=== token_static field coverage ===")
q("token_static NULL/blank per field, ALL 1196 rows", """
SELECT COUNT(*) n,
  SUM(CASE WHEN token_created_at IS NULL THEN 1 ELSE 0 END) null_created_at,
  SUM(CASE WHEN symbol IS NULL OR symbol='' THEN 1 ELSE 0 END) null_symbol,
  SUM(CASE WHEN name IS NULL OR name='' THEN 1 ELSE 0 END) null_name,
  SUM(CASE WHEN decimals IS NULL THEN 1 ELSE 0 END) null_decimals,
  SUM(CASE WHEN launchpad_name IS NULL OR launchpad_name='' THEN 1 ELSE 0 END) null_launchpad,
  SUM(CASE WHEN creator_address IS NULL OR creator_address='' THEN 1 ELSE 0 END) null_creator,
  SUM(CASE WHEN dex_protocol IS NULL OR dex_protocol='' THEN 1 ELSE 0 END) null_dex,
  SUM(CASE WHEN mintable IS NULL THEN 1 ELSE 0 END) null_mintable,
  SUM(CASE WHEN is_scam IS NULL THEN 1 ELSE 0 END) null_isscam,
  SUM(CASE WHEN exchanges_count IS NULL THEN 1 ELSE 0 END) null_exch
 FROM token_static""")

q("token_static coverage per network", """
SELECT network_id, COUNT(*) n,
  SUM(CASE WHEN token_created_at IS NULL THEN 1 ELSE 0 END) null_created_at,
  SUM(CASE WHEN symbol IS NULL OR symbol='' THEN 1 ELSE 0 END) null_symbol,
  SUM(CASE WHEN decimals IS NULL THEN 1 ELSE 0 END) null_decimals,
  SUM(CASE WHEN launchpad_name IS NULL OR launchpad_name='' THEN 1 ELSE 0 END) null_launchpad
 FROM token_static GROUP BY 1 ORDER BY 2 DESC""")

q("ACTIVE watches: token_created_at coverage", """
SELECT COUNT(*) active_watches,
  SUM(CASE WHEN s.token_address IS NULL THEN 1 ELSE 0 END) no_static_row,
  SUM(CASE WHEN s.token_created_at IS NULL THEN 1 ELSE 0 END) null_created_at,
  SUM(CASE WHEN s.symbol IS NULL OR s.symbol='' THEN 1 ELSE 0 END) null_symbol,
  SUM(CASE WHEN s.decimals IS NULL THEN 1 ELSE 0 END) null_decimals
 FROM watchlist w LEFT JOIN token_static s
   ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE w.active=1""")

q("EVERY coin ever in watch_windows: token_created_at coverage", """
SELECT COUNT(*) coins,
  SUM(CASE WHEN s.token_address IS NULL THEN 1 ELSE 0 END) no_static_row,
  SUM(CASE WHEN s.token_created_at IS NULL THEN 1 ELSE 0 END) null_created_at
 FROM (SELECT DISTINCT token_address, network_id FROM watch_windows) w
 LEFT JOIN token_static s ON s.token_address=w.token_address AND s.network_id=w.network_id""")

q("the 12 rows missing token_created_at: who are they", """
SELECT s.token_address, s.network_id, s.recorded_at, s.symbol, s.decimals, s.launchpad_name,
       (SELECT COUNT(*) FROM watch_windows w WHERE w.token_address=s.token_address AND w.network_id=s.network_id) windows,
       (SELECT active FROM watchlist w WHERE w.token_address=s.token_address AND w.network_id=s.network_id) wl_active
  FROM token_static s WHERE s.token_created_at IS NULL""")

q("the 13 token_static rows with no watchlist and no window", """
SELECT s.token_address, s.network_id, s.recorded_at, s.symbol FROM token_static s
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

q("the 91 signal outcomes whose entry_ts is >1h from recorded_at: compare to feed ts", """
SELECT COUNT(*) n,
  SUM(CASE WHEN ABS(o.entry_ts - CAST(strftime('%s',s.ts) AS INTEGER)) <= 60 THEN 1 ELSE 0 END) matches_feed_ts,
  MIN(o.entry_ts - CAST(strftime('%s',s.recorded_at) AS INTEGER)) mn_delta,
  MAX(o.entry_ts - CAST(strftime('%s',s.recorded_at) AS INTEGER)) mx_delta
 FROM outcomes o JOIN signal_events s ON s.id=o.key
 WHERE o.kind='signal'
   AND ABS(o.entry_ts - CAST(strftime('%s',s.recorded_at) AS INTEGER)) > 3600""")

q("all signal outcomes: entry_ts vs feed ts mismatch", """
SELECT COUNT(*) total,
  SUM(CASE WHEN ABS(o.entry_ts - CAST(strftime('%s',s.ts) AS INTEGER)) > 60 THEN 1 ELSE 0 END) mismatch_gt60s
 FROM outcomes o JOIN signal_events s ON s.id=o.key WHERE o.kind='signal'""")

con.close()
