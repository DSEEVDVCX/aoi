import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql):
    t0 = time.time()
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:30]:
        print("   ", dict(r))
    if len(rows) > 30:
        print(f"    ... {len(rows)} rows total")
    return rows

q("training_rows kind=signal whose key is not a signal_events.id", """
SELECT COUNT(*) n FROM training_rows t
 WHERE t.kind='signal'
   AND NOT EXISTS (SELECT 1 FROM signal_events s WHERE s.id = t.key)
""")

q("outcomes kind=signal whose key is not a signal_events.id", """
SELECT COUNT(*) n FROM outcomes o
 WHERE o.kind='signal'
   AND NOT EXISTS (SELECT 1 FROM signal_events s WHERE s.id = o.key)
""")

q("training_rows with no outcomes row (same kind,key)", """
SELECT t.kind, COUNT(*) n FROM training_rows t
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind=t.kind AND o.key=t.key)
 GROUP BY t.kind
""")

q("outcomes with no training_rows row (same kind,key)", """
SELECT o.kind, COUNT(*) n FROM outcomes o
 WHERE NOT EXISTS (SELECT 1 FROM training_rows t WHERE t.kind=o.kind AND t.key=o.key)
 GROUP BY o.kind
""")

q("outcomes kind=watch whose key has no watch_windows row", """
SELECT COUNT(*) n FROM outcomes o
 WHERE o.kind='watch'
   AND NOT EXISTS (SELECT 1 FROM watch_windows w
                    WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at = o.key)
""")

q("watch_windows with no outcomes kind=watch row", """
SELECT COUNT(*) n FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o
                    WHERE o.kind='watch'
                      AND o.key = w.token_address||':'||w.network_id||':'||w.first_seen_at)
""")

q("signal_events with no outcomes row", """
SELECT COUNT(*) n FROM signal_events s
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=s.id)
""")

q("signal_events with no outcomes, by date of recorded_at", """
SELECT substr(s.recorded_at,1,10) d, COUNT(*) n FROM signal_events s
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=s.id)
 GROUP BY 1 ORDER BY 1
""")

q("outcomes coins with no token_static", """
SELECT COUNT(*) rows, COUNT(DISTINCT o.token_address||'|'||COALESCE(o.network_id,'~')) coins
  FROM outcomes o
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
                    WHERE s.token_address=o.token_address
                      AND COALESCE(s.network_id,'')=COALESCE(o.network_id,''))
""")

q("outcomes coins with no token_static, by kind", """
SELECT o.kind, COUNT(*) rows, COUNT(DISTINCT o.token_address||'|'||COALESCE(o.network_id,'~')) coins
  FROM outcomes o
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
                    WHERE s.token_address=o.token_address
                      AND COALESCE(s.network_id,'')=COALESCE(o.network_id,''))
 GROUP BY o.kind
""")

q("training_rows coins with no token_static", """
SELECT t.kind, COUNT(*) rows, COUNT(DISTINCT t.token_address||'|'||COALESCE(t.network_id,'~')) coins
  FROM training_rows t
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
                    WHERE s.token_address=t.token_address
                      AND COALESCE(s.network_id,'')=COALESCE(t.network_id,''))
 GROUP BY t.kind
""")

q("signal_events coins with no token_static", """
SELECT COUNT(*) rows, COUNT(DISTINCT s.token_address||'|'||COALESCE(s.network_id,'~')) coins
  FROM signal_events s
 WHERE NOT EXISTS (SELECT 1 FROM token_static x
                    WHERE x.token_address=s.token_address
                      AND COALESCE(x.network_id,'')=COALESCE(s.network_id,''))
""")

q("signal_events coins with no watch_windows (never watched)", """
SELECT COUNT(*) rows, COUNT(DISTINCT s.token_address||'|'||COALESCE(s.network_id,'~')) coins
  FROM signal_events s
 WHERE NOT EXISTS (SELECT 1 FROM watch_windows w
                    WHERE w.token_address=s.token_address
                      AND COALESCE(w.network_id,'')=COALESCE(s.network_id,''))
""")

con.close()
