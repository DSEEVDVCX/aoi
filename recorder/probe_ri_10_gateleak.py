import os, sys, sqlite3, config, time
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, cap=40):
    t = time.time()
    try: rows = con.execute(sql).fetchall()
    except Exception as e:
        print(f"\n### {label}\nFAILED: {e}"); return
    print(f"\n### {label}   ({time.time()-t:.1f}s)")
    for r in rows[:cap]: print("   ", dict(r))
    if len(rows) > cap: print(f"    ... {len(rows)} rows")

# Are the under-2-day windows on 08-22 FIRST windows (gate leak) or repeats
# on already-admitted coins (comparison-window path bypasses the gate)?
q("under-2-day windows on 2026-08-22: first window for the coin, or a repeat?", """
SELECT CASE WHEN w.first_seen_at = (SELECT MIN(p.first_seen_at) FROM watch_windows p
              WHERE p.token_address=w.token_address AND p.network_id=w.network_id)
            THEN 'FIRST window (gate leak)' ELSE 'repeat window on admitted coin' END cls,
       COUNT(*) n, ROUND(MIN(age_d),3) youngest_days, w.is_control
  FROM (SELECT w.*, (CAST(strftime('%s', w.first_seen_at) AS INTEGER)
                     - CAST(s.token_created_at AS INTEGER))/86400.0 age_d
          FROM watch_windows w JOIN token_static s
            ON s.token_address=w.token_address AND s.network_id=w.network_id
         WHERE w.first_seen_at >= '2026-08-22') w
 WHERE w.age_d < 2.0
 GROUP BY cls, w.is_control
""")

q("all-time: FIRST windows whose coin was < 2 days old at admission, by day", """
SELECT SUBSTR(first_seen_at,1,10) d, COUNT(*) first_windows_under_2d
  FROM (SELECT w.token_address, w.network_id, MIN(w.first_seen_at) first_seen_at,
               MIN((CAST(strftime('%s', w.first_seen_at) AS INTEGER)
                    - CAST(s.token_created_at AS INTEGER))/86400.0) age_d
          FROM watch_windows w JOIN token_static s
            ON s.token_address=w.token_address AND s.network_id=w.network_id
         GROUP BY w.token_address, w.network_id)
 WHERE age_d < 2.0 GROUP BY d ORDER BY d DESC LIMIT 8
""")

q("repeat comparison windows opened on 2026-08-22 for coins now under 2 days old", """
SELECT w.token_address, w.network_id, s.symbol,
       ROUND((CAST(strftime('%s', w.first_seen_at) AS INTEGER)
              - CAST(s.token_created_at AS INTEGER))/86400.0, 3) age_d_at_window,
       COUNT(*) windows_that_day
  FROM watch_windows w JOIN token_static s
    ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE w.first_seen_at >= '2026-08-22'
   AND (CAST(strftime('%s', w.first_seen_at) AS INTEGER)
        - CAST(s.token_created_at AS INTEGER))/86400.0 < 2.0
 GROUP BY w.token_address, w.network_id ORDER BY age_d_at_window
""")

q("fv8 stale rows by network and kind (the EVM freeze inventory)", """
SELECT COALESCE(network_id,'<NULL>') net, kind, COUNT(*) n, MAX(built_at) newest
  FROM training_rows WHERE feature_version=8 GROUP BY net, kind ORDER BY n DESC
""")

q("model population: how big would it be if the EVM freeze were lifted (upper bound)", """
SELECT COUNT(*) reachable_model_rows FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
   AND o.entry_ts >= %d
""" % config.LIVE_START_TS)

q("asset_class NULL / missing token_class rows for coins in outcomes", """
SELECT COUNT(*) coins_ever_in_outcomes,
       SUM(NOT EXISTS (SELECT 1 FROM token_class c
             WHERE c.token_address=x.token_address
               AND COALESCE(c.network_id,'')=COALESCE(x.network_id,''))) coins_without_class
  FROM (SELECT DISTINCT token_address, network_id FROM outcomes) x
""")
q("training_rows.asset_class NULL count", """
SELECT COUNT(*) total, SUM(asset_class IS NULL) null_class FROM training_rows
""")
con.close()
