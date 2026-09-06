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
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print(f"    ... {len(rows)} rows total")
    return rows

q("token_bars mixed-case addresses: who are they", """
SELECT token_address, network_id, COUNT(*) n, MIN(ts) mn, MAX(ts) mx
  FROM token_bars
 WHERE token_address <> lower(token_address) AND substr(token_address,1,2) IN ('0x','0X')
 GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20""")

q("are any mixed-case token_bars coins also in watch_windows lowercase?", """
SELECT COUNT(DISTINCT b.token_address) n
  FROM token_bars b
 WHERE b.token_address <> lower(b.token_address)
   AND EXISTS (SELECT 1 FROM watch_windows w WHERE lower(w.token_address)=lower(b.token_address))""")

q("watch_windows coins with NO token_bars under exact case but bars exist under lower()", """
SELECT COUNT(DISTINCT w.token_address||'|'||w.network_id) n
  FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM token_bars b WHERE b.token_address=w.token_address AND b.network_id=w.network_id)
   AND EXISTS (SELECT 1 FROM token_bars b WHERE lower(b.token_address)=lower(w.token_address) AND b.network_id=w.network_id)""")

q("watch_windows coins with no token_bars at all (exact or lower)", """
SELECT COUNT(DISTINCT w.token_address||'|'||w.network_id) n
  FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM token_bars b WHERE lower(b.token_address)=lower(w.token_address) AND b.network_id=w.network_id)""")

print()
print("=== DUPLICATES ===")
q("duplicate signal_events (token, ts, signal_type) -- groups and extra rows", """
SELECT COUNT(*) dup_groups, SUM(c-1) extra_rows, SUM(c) rows_in_dup_groups
  FROM (SELECT token_address, COALESCE(network_id,'~') n, ts, signal_type, COUNT(*) c
          FROM signal_events GROUP BY 1,2,3,4 HAVING c>1)""")

q("worst duplicate signal_events groups", """
SELECT token_address, network_id, ts, signal_type, COUNT(*) c
  FROM signal_events GROUP BY 1,2,3,4 HAVING c>1 ORDER BY c DESC LIMIT 10""")

q("duplicate signal_events by (token,ts,signal_type) restricted to rows that are in training_rows population", """
SELECT COUNT(*) dup_groups, SUM(c-1) extra_rows
  FROM (SELECT s.token_address, s.ts, s.signal_type, COUNT(*) c
          FROM signal_events s
          JOIN training_rows t ON t.kind='signal' AND t.key=s.id
         WHERE t.is_live=1 AND t.asset_class='meme' AND t.status='ok' AND t.is_independent=1
           AND t.feature_version=CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
         GROUP BY 1,2,3 HAVING c>1)""")

q("training_rows keys shared across kinds", """
SELECT COUNT(*) keys_in_2plus_kinds FROM (SELECT key, COUNT(DISTINCT kind) k FROM training_rows GROUP BY key HAVING k>1)""")
q("outcomes keys shared across kinds", """
SELECT COUNT(*) keys_in_2plus_kinds FROM (SELECT key, COUNT(DISTINCT kind) k FROM outcomes GROUP BY key HAVING k>1)""")

q("overlapping watch_windows for one coin (new window opened before previous closed)", """
SELECT COUNT(*) overlapping_pairs, COUNT(DISTINCT token_address||'|'||network_id) coins
  FROM (SELECT w.token_address, w.network_id, w.first_seen_at,
               LAG(w.watch_until) OVER (PARTITION BY w.token_address, w.network_id ORDER BY w.first_seen_at) prev_until
          FROM watch_windows w)
 WHERE prev_until IS NOT NULL AND first_seen_at < prev_until""")

q("watch_windows per coin distribution", """
SELECT c windows_per_coin, COUNT(*) coins FROM
 (SELECT token_address, network_id, COUNT(*) c FROM watch_windows GROUP BY 1,2)
 GROUP BY 1 ORDER BY 1 DESC LIMIT 12""")

con.close()
