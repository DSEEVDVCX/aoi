import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, cap=30):
    t = time.time()
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}"); return
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    for r in rows[:cap]: print("   ", dict(r))
    if len(rows) > cap: print(f"    ... {len(rows)} rows")

print("========== EVM FREEZE IMPACT ON MODEL POPULATION ==========")
q("signal outcomes that WOULD be model rows but have no current-fv training row", """
SELECT COALESCE(o.network_id,'<NULL>') net, COUNT(*) blocked
  FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
   AND o.entry_ts >= %d
   AND NOT EXISTS (SELECT 1 FROM training_rows r
                    WHERE r.kind='signal' AND r.key=o.key
                      AND r.feature_version = CAST(COALESCE(
                          (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER))
 GROUP BY net ORDER BY blocked DESC
""" % config.LIVE_START_TS)

q("same, total + how many already have a STALE (fv<current) row", """
SELECT COUNT(*) blocked_total,
       SUM(EXISTS (SELECT 1 FROM training_rows r WHERE r.kind='signal' AND r.key=o.key)) has_stale_row,
       SUM(NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind='signal' AND r.key=o.key)) never_built
  FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
   AND o.entry_ts >= %d
   AND NOT EXISTS (SELECT 1 FROM training_rows r
                    WHERE r.kind='signal' AND r.key=o.key
                      AND r.feature_version = CAST(COALESCE(
                          (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER))
""" % config.LIVE_START_TS)

q("current model population by network (what the freeze already cost)", """
SELECT COALESCE(network_id,'<NULL>') net, COUNT(*) rows, COUNT(DISTINCT token_address) coins
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
 GROUP BY net ORDER BY rows DESC
""")

print("\n========== is_live ON NON-SIGNAL KINDS ==========")
q("is_live by kind vs entry_ts>=LIVE_START_TS", f"""
SELECT kind, COUNT(*) rows,
       SUM(entry_ts >= {config.LIVE_START_TS}) after_live_start,
       SUM(is_live=1) marked_live
  FROM training_rows GROUP BY kind
""")
q("control watch rows in training_rows (is_control lives in outcomes)", """
SELECT o.is_control, COUNT(*) rows, SUM(tr.is_live) live
  FROM training_rows tr JOIN outcomes o ON o.kind=tr.kind AND o.key=tr.key
 WHERE tr.kind='watch' GROUP BY o.is_control
""")

print("\n========== PHASE-1 WATCH COMPARISON CONCENTRATION ==========")
q("phase1_watch_outcomes: size by is_control", """
SELECT is_control, COUNT(*) rows, COUNT(DISTINCT token_address) coins
  FROM phase1_watch_outcomes GROUP BY is_control
""")
q("phase1_watch_outcomes: top 10 coins by row share", """
SELECT token_address, network_id, is_control, COUNT(*) rows,
       ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM phase1_watch_outcomes),2) pct_of_all
  FROM phase1_watch_outcomes GROUP BY 1,2,3 ORDER BY rows DESC LIMIT 10
""")
q("phase1: share held by the top 10 coins", """
SELECT SUM(rows) top10_rows, (SELECT COUNT(*) FROM phase1_watch_outcomes) total,
       ROUND(100.0*SUM(rows)/(SELECT COUNT(*) FROM phase1_watch_outcomes),1) pct
  FROM (SELECT COUNT(*) rows FROM phase1_watch_outcomes
         GROUP BY token_address, network_id ORDER BY rows DESC LIMIT 10)
""")
q("watch outcomes: overlapping-window share (entry within 48h of an earlier watch on same coin)", """
SELECT COUNT(*) overlapping FROM outcomes o
 WHERE o.kind='watch'
   AND EXISTS (SELECT 1 FROM outcomes p
                WHERE p.kind='watch' AND p.token_address=o.token_address
                  AND COALESCE(p.network_id,'')=COALESCE(o.network_id,'')
                  AND p.entry_ts < o.entry_ts AND o.entry_ts - p.entry_ts < 48*3600)
""")

print("\n========== NEGATIVE DRIFT MAGNITUDE ==========")
q("negative drift magnitude buckets", """
SELECT CASE WHEN d >= -2 THEN '-2..0s (rounding)'
            WHEN d >= -60 THEN '-60..-2s'
            WHEN d >= -300 THEN '-5min..-60s'
            ELSE 'worse than -5min' END b, COUNT(*) n, MIN(d) worst
  FROM (SELECT o.entry_ts - CAST(strftime('%s', e.ts) AS INTEGER) d
          FROM outcomes o JOIN signal_events e ON e.id=o.key AND o.kind='signal')
 WHERE d < 0 GROUP BY b ORDER BY n DESC
""")
q("the 5 coins whose token_created_at postdates their first window", """
SELECT s.token_address, s.network_id, s.symbol, s.token_created_at,
       MIN(w.first_seen_at) first_window,
       ROUND((CAST(s.token_created_at AS INTEGER)
              - CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER))/3600.0,2) hours_after
  FROM watch_windows w JOIN token_static s
    ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE s.token_created_at IS NOT NULL
 GROUP BY s.token_address, s.network_id
HAVING CAST(s.token_created_at AS INTEGER) > CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER)
""")
q("the 12 coins still missing token_created_at: were they ever watched / do they have model rows", """
SELECT s.token_address, s.network_id, s.symbol,
       (SELECT COUNT(*) FROM watch_windows w WHERE w.token_address=s.token_address
          AND w.network_id=s.network_id) windows,
       (SELECT COUNT(*) FROM training_rows t WHERE t.token_address=s.token_address
          AND t.kind='signal') signal_rows
  FROM token_static s WHERE s.token_created_at IS NULL
""")
con.close()
