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

q("FIRST windows since the gate (2026-08-21+): age at admission, control vs signal arm", """
SELECT is_control, COUNT(*) first_windows,
       SUM(age_d < 2.0) under_2_days,
       ROUND(MIN(age_d),3) youngest_days, ROUND(AVG(age_d),1) avg_days
  FROM (SELECT w.is_control, MIN(w.first_seen_at) fs,
               (CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER)
                - CAST(s.token_created_at AS INTEGER))/86400.0 age_d
          FROM watch_windows w JOIN token_static s
            ON s.token_address=w.token_address AND s.network_id=w.network_id
         GROUP BY w.token_address, w.network_id
        HAVING MIN(w.first_seen_at) >= '2026-08-21')
 GROUP BY is_control
""")

q("control arm all-time: first-window age distribution", """
SELECT CASE WHEN age_d < 1 THEN '<1 day' WHEN age_d < 2 THEN '1-2 days'
            WHEN age_d < 7 THEN '2-7 days' ELSE '>7 days' END b, COUNT(*) coins
  FROM (SELECT (CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER)
                - CAST(s.token_created_at AS INTEGER))/86400.0 age_d
          FROM watch_windows w JOIN token_static s
            ON s.token_address=w.token_address AND s.network_id=w.network_id
         WHERE w.is_control=1 GROUP BY w.token_address, w.network_id)
 GROUP BY b ORDER BY 1
""")

q("training_rows watch rows: asset_class + is_live (both arms)", """
SELECT o.is_control, COUNT(*) rows, SUM(tr.asset_class IS NULL) asset_class_null,
       SUM(tr.is_live=0) is_live_zero
  FROM training_rows tr JOIN outcomes o ON o.kind=tr.kind AND o.key=tr.key
 WHERE tr.kind='watch' GROUP BY o.is_control
""")

q("token_class table: does features.py even use it? coins classified vs coins in outcomes", """
SELECT (SELECT COUNT(*) FROM token_class) token_class_rows,
       (SELECT COUNT(DISTINCT token_address||'|'||COALESCE(network_id,'')) FROM outcomes) coins_in_outcomes
""")
con.close()
