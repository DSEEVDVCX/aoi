import os, sys, sqlite3, config, time
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, cap=30):
    t = time.time()
    try: rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}"); return
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    for r in rows[:cap]: print("   ", dict(r))
    if len(rows) > cap: print(f"    ... {len(rows)} rows")

q("coins with NULL token_created_at: how many, and their training rows", """
SELECT COUNT(*) coins,
       (SELECT COUNT(*) FROM training_rows t JOIN token_static s
          ON s.token_address=t.token_address AND COALESCE(s.network_id,'')=COALESCE(t.network_id,'')
        WHERE s.token_created_at IS NULL) training_rows_affected,
       (SELECT COUNT(*) FROM training_rows t JOIN token_static s
          ON s.token_address=t.token_address AND COALESCE(s.network_id,'')=COALESCE(t.network_id,'')
        WHERE s.token_created_at IS NULL AND t.kind='signal' AND t.is_live=1
          AND t.asset_class='meme' AND t.status='ok' AND t.is_independent=1
          AND t.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
       ) model_rows_affected
  FROM token_static WHERE token_created_at IS NULL
""")

q("token_age_h NULL inside the model population", """
SELECT COUNT(*) model_rows, SUM(token_age_h IS NULL) age_null,
       ROUND(100.0*SUM(token_age_h IS NULL)/COUNT(*),2) pct
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
""")

q("AGE GATE: windows opened on/after 2026-08-22 vs coin age at admission", """
SELECT SUM(age_d < 2.0) under_2_days, SUM(age_d >= 2.0) ok_2plus,
       SUM(age_d IS NULL) age_unknown, COUNT(*) windows_since_gate,
       ROUND(MIN(age_d),3) min_age_d
  FROM (SELECT (CAST(strftime('%s', w.first_seen_at) AS INTEGER)
                - CAST(s.token_created_at AS INTEGER))/86400.0 age_d
          FROM watch_windows w LEFT JOIN token_static s
            ON s.token_address=w.token_address AND s.network_id=w.network_id
         WHERE w.first_seen_at >= '2026-08-22')
""")
q("AGE GATE: same, day by day around the gate", """
SELECT SUBSTR(w.first_seen_at,1,10) d, COUNT(*) windows,
       SUM(((CAST(strftime('%s', w.first_seen_at) AS INTEGER)
             - CAST(s.token_created_at AS INTEGER))/86400.0) < 2.0) under_2d,
       SUM(s.token_created_at IS NULL) age_unknown
  FROM watch_windows w LEFT JOIN token_static s
    ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE w.first_seen_at >= '2026-08-19'
 GROUP BY d ORDER BY d
""")

q("watch outcomes total (denominator for the 22,640 overlap)", """
SELECT COUNT(*) watch_outcomes FROM outcomes WHERE kind='watch'
""")
q("watch_windows total (denominator for 26,966 overlap)", "SELECT COUNT(*) n FROM watch_windows")
q("control arm: windows per control coin", """
SELECT windows_per_coin, COUNT(*) coins FROM (
  SELECT token_address, COUNT(*) windows_per_coin FROM watch_windows
   WHERE is_control=1 GROUP BY token_address, network_id)
 GROUP BY 1 ORDER BY 1
""")
q("signal arm: windows per signal-sourced coin (stats)", """
SELECT COUNT(*) coins, SUM(w) windows, ROUND(AVG(w),1) avg_windows, MAX(w) max_windows
  FROM (SELECT COUNT(*) w FROM watch_windows WHERE is_control=0
         GROUP BY token_address, network_id)
""")
q("training_rows built_at recency (is the builder alive?)", """
SELECT MAX(built_at) newest_built, MIN(built_at) oldest_built FROM training_rows
""")
q("newest built_at per feature_version", """
SELECT feature_version, MAX(built_at) newest, COUNT(*) n FROM training_rows GROUP BY 1
""")
q("meta: recorder/builder heartbeats", """
SELECT key, CAST(value AS TEXT) v FROM meta
 WHERE key LIKE '%last_run%' OR key LIKE '%_ok_at%' OR key LIKE '%heartbeat%' ORDER BY key
""", cap=40)
con.close()
