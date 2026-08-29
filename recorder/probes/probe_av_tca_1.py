import os, sqlite3, config, sys
from features import epoch_of

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row


def q(label, sql, args=()):
    print("=== " + label + " ===")
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print("  FAILED:", e)
        return []
    for r in rows[:40]:
        print("  " + " | ".join(f"{k}={r[k]}" for k in r.keys()))
    if not rows:
        print("  (no rows)")
    print()
    return rows


# A) my own independent count of token_static coverage
q("A token_static coverage (my own phrasing: CASE WHEN, incl. empty-string)", """
SELECT COUNT(*) AS n_rows,
       COUNT(DISTINCT token_address || '|' || network_id) AS n_distinct_keys,
       SUM(CASE WHEN token_created_at IS NULL THEN 1 ELSE 0 END) AS created_is_null,
       SUM(CASE WHEN token_created_at = '' THEN 1 ELSE 0 END) AS created_is_empty,
       SUM(CASE WHEN COALESCE(token_created_at,'') = '' THEN 1 ELSE 0 END) AS created_null_or_empty
  FROM token_static""")

# B) format families of token_created_at (does CAST work at all?)
q("B token_created_at format families", """
SELECT CASE WHEN token_created_at IS NULL THEN 'NULL'
            WHEN token_created_at = '' THEN 'EMPTY'
            WHEN token_created_at GLOB '*[^0-9.]*' THEN 'non-numeric(ISO?)'
            ELSE 'numeric' END AS fam,
       COUNT(*) AS n, MIN(token_created_at) AS mn, MAX(token_created_at) AS mx
  FROM token_static GROUP BY 1 ORDER BY n DESC""")

# C) first_seen_at format families -- does strftime('%s',...) even work?
q("C watch_windows.first_seen_at format + strftime sanity", """
SELECT COUNT(*) AS n,
       SUM(CASE WHEN first_seen_at GLOB '*[^0-9.]*' THEN 1 ELSE 0 END) AS non_numeric,
       SUM(CASE WHEN strftime('%s', first_seen_at) IS NULL THEN 1 ELSE 0 END) AS strftime_null,
       MIN(first_seen_at) AS mn, MAX(first_seen_at) AS mx
  FROM watch_windows""")

q("C2 distinct first_seen_at suffix shapes", """
SELECT CASE WHEN first_seen_at LIKE '%Z' THEN 'endsZ'
            WHEN first_seen_at LIKE '%+00:00' THEN 'ends+00:00'
            ELSE 'other' END AS shape, COUNT(*) n
  FROM watch_windows GROUP BY 1""")

# D) coins ever watched, exact + case-insensitive join
q("D coins ever watched vs token_static (case sensitivity check)", """
WITH w AS (SELECT DISTINCT token_address, network_id FROM watch_windows)
SELECT (SELECT COUNT(*) FROM w) AS distinct_watched,
       (SELECT COUNT(*) FROM w JOIN token_static s
          ON s.token_address = w.token_address AND s.network_id = w.network_id) AS matched_exact,
       (SELECT COUNT(*) FROM w JOIN token_static s
          ON lower(s.token_address) = lower(w.token_address)
         AND s.network_id = w.network_id) AS matched_ci,
       (SELECT COUNT(*) FROM w JOIN token_static s
          ON s.token_address = w.token_address AND s.network_id = w.network_id
         WHERE s.token_created_at IS NULL) AS watched_null_created""")

# E) the NULL-created coins: who are they, were they watched, when recorded
rows = q("E the coins with NULL token_created_at", """
SELECT s.token_address, s.network_id, s.symbol, s.recorded_at,
       (SELECT COUNT(*) FROM watch_windows w
         WHERE w.token_address = s.token_address AND w.network_id = s.network_id) AS windows,
       (SELECT COUNT(*) FROM signal_events e
         WHERE e.token_address = s.token_address
           AND COALESCE(e.network_id,'') = s.network_id) AS sig_events
  FROM token_static s WHERE COALESCE(s.token_created_at,'') = ''
 ORDER BY s.recorded_at""")

con.close()
