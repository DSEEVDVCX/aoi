import os, sys, sqlite3, config
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== THEIR QUERY 1 verbatim (cutoff 2026-08-21) ===")
for r in con.execute("""
SELECT is_control, COUNT(*) first_windows, SUM(age_d < 2.0) under_2_days,
       ROUND(MIN(age_d),3) youngest_days
  FROM (SELECT w.is_control,
               (CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER)
                - CAST(s.token_created_at AS INTEGER))/86400.0 age_d
          FROM watch_windows w
          JOIN token_static s ON s.token_address=w.token_address
                             AND s.network_id=w.network_id
         GROUP BY w.token_address, w.network_id
        HAVING MIN(w.first_seen_at) >= '2026-08-21')
 GROUP BY is_control"""):
    print("  ", dict(r))

print("\n=== THEIR QUERY 2 verbatim (all-time control buckets) ===")
for r in con.execute("""
SELECT CASE WHEN age_d<1 THEN 'a <1 day' WHEN age_d<2 THEN 'b 1-2 days'
            WHEN age_d<7 THEN 'c 2-7 days' ELSE 'd >7 days' END b, COUNT(*) coins
  FROM (SELECT (CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER)
                - CAST(s.token_created_at AS INTEGER))/86400.0 age_d
          FROM watch_windows w
          JOIN token_static s ON s.token_address=w.token_address
                             AND s.network_id=w.network_id
         WHERE w.is_control=1
         GROUP BY w.token_address, w.network_id)
 GROUP BY b"""):
    print("  ", dict(r))

print("\n=== SAME query but for the SIGNAL arm (the comparison they never ran) ===")
for r in con.execute("""
SELECT CASE WHEN age_d<1 THEN 'a <1 day' WHEN age_d<2 THEN 'b 1-2 days'
            WHEN age_d<7 THEN 'c 2-7 days' ELSE 'd >7 days' END b, COUNT(*) coins
  FROM (SELECT (CAST(strftime('%s', MIN(w.first_seen_at)) AS INTEGER)
                - CAST(s.token_created_at AS INTEGER))/86400.0 age_d
          FROM watch_windows w
          JOIN token_static s ON s.token_address=w.token_address
                             AND s.network_id=w.network_id
         WHERE w.is_control=0
         GROUP BY w.token_address, w.network_id)
 GROUP BY b"""):
    print("  ", dict(r))

GATE = "2026-08-22T13:46:30"
print("\n=== post-gate SIGNAL-arm windows (any, not just first) on sub-2-day coins:"
      " was the coin admitted before the gate? ===")
for r in con.execute("""
SELECT COUNT(*) windows,
       COUNT(DISTINCT w.token_address||':'||w.network_id) coins,
       SUM(CASE WHEN f.first_ever < ? THEN 1 ELSE 0 END) coin_admitted_pre_gate
  FROM watch_windows w
  JOIN token_static s ON s.token_address=w.token_address AND s.network_id=w.network_id
  JOIN (SELECT token_address, network_id, MIN(first_seen_at) first_ever
          FROM watch_windows GROUP BY token_address, network_id) f
    ON f.token_address=w.token_address AND f.network_id=w.network_id
 WHERE w.is_control=0 AND w.first_seen_at >= ?
   AND CAST(s.token_created_at AS INTEGER) > 0
   AND (CAST(strftime('%s', w.first_seen_at) AS INTEGER)
        - CAST(s.token_created_at AS INTEGER))/86400.0 BETWEEN 0 AND 2.0
""", (GATE, GATE)):
    print("  ", dict(r))

print("\n=== how much of the phase1 comparison population has MATURED so far,"
      " and how much of it was admitted post-gate ===")
for r in con.execute("""
SELECT o.is_control, COUNT(*) outcomes,
       SUM(CASE WHEN w.first_seen_at >= ? THEN 1 ELSE 0 END) post_gate
  FROM outcomes o
  JOIN watch_windows w
    ON w.token_address||':'||w.network_id||':'||w.first_seen_at = o.key
   AND w.design_version >= 3 AND w.admission_source IN ('trending','verified')
  JOIN phase1_watch_outcomes e ON e.kind=o.kind AND e.key=o.key
 WHERE o.kind='watch'
 GROUP BY o.is_control""", (GATE,)):
    print("  ", dict(r))
con.close()
