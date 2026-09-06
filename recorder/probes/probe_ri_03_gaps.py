import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql):
    t = time.time()
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}"); return
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    for r in rows[:50]:
        print("   ", dict(r))
    if len(rows) > 50: print(f"    ... {len(rows)} rows")

q("the 43 cross-kind keys: which kind pairs, sample keys", """
SELECT kinds, COUNT(*) n, MIN(key) sample_key FROM (
  SELECT key, GROUP_CONCAT(DISTINCT kind) kinds
    FROM outcomes GROUP BY key HAVING COUNT(DISTINCT kind) > 1
) GROUP BY kinds
""")

q("sample of cross-kind rows in outcomes", """
SELECT o.kind, o.key, o.token_address, o.network_id, o.entry_ts, o.status, o.is_control, o.split
  FROM outcomes o
 WHERE o.key IN (SELECT key FROM outcomes GROUP BY key HAVING COUNT(DISTINCT kind)>1)
 ORDER BY o.key, o.kind LIMIT 20
""")

q("outcomes kind=signal status=ok with NO training row: built_at era / feature_version of neighbours", """
SELECT MIN(o.entry_ts) min_entry, MAX(o.entry_ts) max_entry, COUNT(*) n,
       MIN(o.labeled_at) min_lab, MAX(o.labeled_at) max_lab
  FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok'
   AND NOT EXISTS (SELECT 1 FROM training_rows tr WHERE tr.kind='signal' AND tr.key=o.key)
""")

q("same, bucketed by day of entry_ts", """
SELECT DATE(o.entry_ts,'unixepoch') d, COUNT(*) missing
  FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok'
   AND NOT EXISTS (SELECT 1 FROM training_rows tr WHERE tr.kind='signal' AND tr.key=o.key)
 GROUP BY d ORDER BY d
""")

q("for comparison: outcomes kind=signal ok TOTAL by day", """
SELECT DATE(entry_ts,'unixepoch') d, COUNT(*) total
  FROM outcomes WHERE kind='signal' AND status='ok'
 GROUP BY d ORDER BY d
""")

q("training_rows feature_version distribution", """
SELECT feature_version, kind, COUNT(*) n FROM training_rows GROUP BY feature_version, kind ORDER BY feature_version, kind
""")

q("995 signal_events with no watch_window: date range + are they age-gated?", """
SELECT DATE(e.recorded_at) d, COUNT(*) n, COUNT(DISTINCT e.token_address) tok
  FROM signal_events e
 WHERE NOT EXISTS (SELECT 1 FROM watch_windows w
                    WHERE w.token_address=e.token_address
                      AND COALESCE(w.network_id,'')=COALESCE(e.network_id,''))
 GROUP BY d ORDER BY d
""")

q("do those 995 have outcomes rows anyway?", """
SELECT COUNT(*) n FROM signal_events e
 WHERE NOT EXISTS (SELECT 1 FROM watch_windows w
                    WHERE w.token_address=e.token_address
                      AND COALESCE(w.network_id,'')=COALESCE(e.network_id,''))
   AND EXISTS (SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=e.id)
""")

con.close()
