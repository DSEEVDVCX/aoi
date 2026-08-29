import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql):
    t = time.time()
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}\nSQL: {sql}")
        return
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    print("SQL:", " ".join(sql.split()))
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print(f"    ... {len(rows)} rows total")

q("distinct tokens per table", """
SELECT 'signal_events' t, COUNT(DISTINCT token_address||'|'||COALESCE(network_id,'~')) n FROM signal_events
UNION ALL SELECT 'watchlist', COUNT(DISTINCT token_address||'|'||COALESCE(network_id,'~')) FROM watchlist
UNION ALL SELECT 'watch_windows', COUNT(DISTINCT token_address||'|'||COALESCE(network_id,'~')) FROM watch_windows
UNION ALL SELECT 'token_static', COUNT(DISTINCT token_address||'|'||COALESCE(network_id,'~')) FROM token_static
UNION ALL SELECT 'outcomes', COUNT(DISTINCT token_address||'|'||COALESCE(network_id,'~')) FROM outcomes
UNION ALL SELECT 'training_rows', COUNT(DISTINCT token_address||'|'||COALESCE(network_id,'~')) FROM training_rows
""")

q("watch_windows: rows+tokens with NO token_static row (token+network)", """
SELECT COUNT(*) windows,
       COUNT(DISTINCT w.token_address||'|'||COALESCE(w.network_id,'~')) tokens
  FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
                    WHERE s.token_address = w.token_address
                      AND COALESCE(s.network_id,'') = COALESCE(w.network_id,''))
""")

q("watch_windows missing static, split by is_control", """
SELECT w.is_control, COUNT(*) windows,
       COUNT(DISTINCT w.token_address||'|'||COALESCE(w.network_id,'~')) tokens
  FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
                    WHERE s.token_address = w.token_address
                      AND COALESCE(s.network_id,'') = COALESCE(w.network_id,''))
 GROUP BY w.is_control
""")

q("watchlist rows with NO watch_windows row (same token+network)", """
SELECT COUNT(*) n FROM watchlist wl
 WHERE NOT EXISTS (SELECT 1 FROM watch_windows w
                    WHERE w.token_address = wl.token_address
                      AND w.network_id = wl.network_id)
""")

q("watchlist rows with NO token_static", """
SELECT COUNT(*) n, SUM(active) active_n FROM watchlist wl
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
                    WHERE s.token_address = wl.token_address
                      AND s.network_id = wl.network_id)
""")

q("outcomes kind=signal whose key is not a signal_events.id", """
SELECT COUNT(*) n FROM outcomes o
 WHERE o.kind='signal'
   AND NOT EXISTS (SELECT 1 FROM signal_events e WHERE e.id = o.key)
""")

q("training_rows kind=signal whose key is not a signal_events.id", """
SELECT COUNT(*) n FROM training_rows tr
 WHERE tr.kind='signal'
   AND NOT EXISTS (SELECT 1 FROM signal_events e WHERE e.id = tr.key)
""")

q("training_rows with no matching outcomes (kind,key)", """
SELECT tr.kind, COUNT(*) n FROM training_rows tr
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind=tr.kind AND o.key=tr.key)
 GROUP BY tr.kind
""")

q("outcomes with no matching training_rows (kind,key)", """
SELECT o.kind, o.status, COUNT(*) n FROM outcomes o
 WHERE NOT EXISTS (SELECT 1 FROM training_rows tr WHERE tr.kind=o.kind AND tr.key=o.key)
 GROUP BY o.kind, o.status
""")

q("kind breakdown outcomes", "SELECT kind, status, COUNT(*) n FROM outcomes GROUP BY kind, status ORDER BY kind, n DESC")
q("kind breakdown training_rows", "SELECT kind, status, COUNT(*) n FROM training_rows GROUP BY kind, status ORDER BY kind, n DESC")

q("signal_events with no watch_window for its token (ever watched?)", """
SELECT COUNT(*) events,
       COUNT(DISTINCT e.token_address||'|'||COALESCE(e.network_id,'~')) tokens
  FROM signal_events e
 WHERE NOT EXISTS (SELECT 1 FROM watch_windows w
                    WHERE w.token_address = e.token_address
                      AND COALESCE(w.network_id,'') = COALESCE(e.network_id,''))
""")

q("training_rows keys shared across kinds", """
SELECT COUNT(*) keys_in_2plus_kinds FROM (
  SELECT key FROM training_rows GROUP BY key HAVING COUNT(DISTINCT kind) > 1
)
""")
q("outcomes keys shared across kinds", """
SELECT COUNT(*) keys_in_2plus_kinds FROM (
  SELECT key FROM outcomes GROUP BY key HAVING COUNT(DISTINCT kind) > 1
)
""")

con.close()
