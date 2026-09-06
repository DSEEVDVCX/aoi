import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

print("=== A. basic shape of watch_windows ===")
r = q("""SELECT COUNT(*) n,
                COUNT(DISTINCT token_address||':'||network_id) coins,
                MIN(first_seen_at) lo, MAX(first_seen_at) hi
           FROM watch_windows""")[0]
print(dict(r))

print("\n=== B. per arm / per source ===")
for r in q("""SELECT is_control, source, COUNT(*) n,
                     COUNT(DISTINCT token_address||':'||network_id) coins,
                     MIN(design_version) dvmin, MAX(design_version) dvmax
                FROM watch_windows GROUP BY is_control, source ORDER BY n DESC"""):
    print(dict(r))

print("\n=== C. MY OWN overlap measure: window function MAX(prev watch_until) ===")
r = q("""WITH w AS (
           SELECT token_address, network_id, first_seen_at, watch_until, is_control, source,
                  MAX(watch_until) OVER (
                    PARTITION BY token_address, network_id
                    ORDER BY first_seen_at
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                  ) AS prev_max_until
             FROM watch_windows
         )
         SELECT COUNT(*) total,
                SUM(prev_max_until IS NOT NULL AND prev_max_until > first_seen_at) overlapping,
                SUM(prev_max_until IS NOT NULL) not_first
           FROM w""")[0]
print(dict(r), " pct=", round(100.0*r["overlapping"]/r["total"], 2))

print("\n=== C2. same but case-normalized token address (EVM mixed case) ===")
r = q("""WITH w AS (
           SELECT lower(token_address) ta, network_id, first_seen_at, watch_until,
                  MAX(watch_until) OVER (
                    PARTITION BY lower(token_address), network_id
                    ORDER BY first_seen_at
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                  ) AS prev_max_until
             FROM watch_windows
         )
         SELECT COUNT(*) total,
                SUM(prev_max_until IS NOT NULL AND prev_max_until > first_seen_at) overlapping
           FROM w""")[0]
print(dict(r), " pct=", round(100.0*r["overlapping"]/r["total"], 2))

print("\n=== D. their exact SQL, for agreement check ===")
r = q("""SELECT COUNT(*) overlapping_windows FROM watch_windows w
          WHERE EXISTS (SELECT 1 FROM watch_windows p
                         WHERE p.token_address=w.token_address AND p.network_id=w.network_id
                           AND p.first_seen_at < w.first_seen_at
                           AND p.watch_until > w.first_seen_at)""")[0]
print(dict(r))

print("\n=== E. overlapping by source/arm ===")
for r in q("""WITH w AS (
           SELECT token_address, network_id, first_seen_at, watch_until, is_control, source,
                  MAX(watch_until) OVER (
                    PARTITION BY token_address, network_id
                    ORDER BY first_seen_at
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                  ) AS prev_max_until
             FROM watch_windows)
         SELECT is_control, source, COUNT(*) n,
                SUM(prev_max_until IS NOT NULL AND prev_max_until > first_seen_at) overl
           FROM w GROUP BY is_control, source ORDER BY n DESC"""):
    print(dict(r))

print("\n=== F. windows-per-coin distribution ===")
for r in q("""WITH pc AS (SELECT is_control, token_address, network_id, COUNT(*) c
                            FROM watch_windows GROUP BY 1,2,3)
              SELECT is_control,
                     COUNT(*) coins, SUM(c) windows,
                     ROUND(AVG(c),2) avg_per_coin, MAX(c) max_per_coin,
                     SUM(c=1) coins_with_1
                FROM pc GROUP BY is_control"""):
    print(dict(r))

print("\n=== G. duration check: is every window exactly 48h? ===")
for r in q("""SELECT is_control, source,
                     COUNT(*) n,
                     SUM(CAST(ROUND((julianday(watch_until)-julianday(first_seen_at))*24) AS INT)=48) h48,
                     MIN(ROUND((julianday(watch_until)-julianday(first_seen_at))*24,3)) minh,
                     MAX(ROUND((julianday(watch_until)-julianday(first_seen_at))*24,3)) maxh
                FROM watch_windows GROUP BY 1,2 ORDER BY n DESC"""):
    print(dict(r))

print("\n=== H. top coins by window count ===")
for r in q("""SELECT token_address, network_id, is_control, source, COUNT(*) c,
                     MIN(first_seen_at) lo, MAX(first_seen_at) hi
                FROM watch_windows GROUP BY 1,2 ORDER BY c DESC LIMIT 8"""):
    print(dict(r))

con.close()
