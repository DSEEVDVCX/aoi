import os, sys, io, sqlite3, config
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from features import epoch_of

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v"
).fetchone()["v"]
print("current_feature_version =", FV)
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")


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


# 1) model population size and token_age_h coverage -- my own phrasing
q("1 model population size + token_age_h coverage", f"""
SELECT COUNT(*) n,
       SUM(CASE WHEN token_age_h IS NULL THEN 1 ELSE 0 END) age_null,
       ROUND(100.0*SUM(CASE WHEN token_age_h IS NULL THEN 1 ELSE 0 END)/COUNT(*),2) pct_null,
       SUM(CASE WHEN token_age_h < 0 THEN 1 ELSE 0 END) age_negative,
       ROUND(MIN(token_age_h),2) mn, ROUND(MAX(token_age_h),2) mx
  FROM training_rows WHERE {POP}""")

# 2) same, without is_independent, and for ALL training_rows -- population sanity (angle b)
q("2 population sensitivity (which filter moves the pct)", f"""
SELECT 'ALL training_rows' scope, COUNT(*) n,
       SUM(token_age_h IS NULL) age_null,
       ROUND(100.0*SUM(token_age_h IS NULL)/COUNT(*),2) pct FROM training_rows
UNION ALL SELECT 'kind=signal', COUNT(*), SUM(token_age_h IS NULL),
       ROUND(100.0*SUM(token_age_h IS NULL)/COUNT(*),2) FROM training_rows WHERE kind='signal'
UNION ALL SELECT 'kind=signal is_live=1', COUNT(*), SUM(token_age_h IS NULL),
       ROUND(100.0*SUM(token_age_h IS NULL)/COUNT(*),2) FROM training_rows
       WHERE kind='signal' AND is_live=1
UNION ALL SELECT 'model population', COUNT(*), SUM(token_age_h IS NULL),
       ROUND(100.0*SUM(token_age_h IS NULL)/COUNT(*),2) FROM training_rows WHERE {POP}""")

# 3) WHY is token_age_h NULL in the model population? decompose the cause.
q("3 cause decomposition of token_age_h NULL in model population", f"""
WITH p AS (SELECT token_address, network_id, ts, token_age_h FROM training_rows WHERE {POP})
SELECT CASE
         WHEN s.token_address IS NULL THEN 'A no token_static row at all'
         WHEN s.token_created_at IS NULL THEN 'B token_static exists, created_at NULL'
         ELSE 'C created_at present -> negative age suppressed or other'
       END cause,
       COUNT(*) rows_
  FROM p LEFT JOIN token_static s
       ON s.token_address = p.token_address AND s.network_id = p.network_id
 WHERE p.token_age_h IS NULL
 GROUP BY 1 ORDER BY rows_ DESC""")

# 3b) case-insensitive variant of the same join (EVM mixed case trap)
q("3b same decomposition with case-insensitive join", f"""
WITH p AS (SELECT token_address, network_id, ts, token_age_h FROM training_rows WHERE {POP})
SELECT CASE
         WHEN s.token_address IS NULL THEN 'A no token_static row at all'
         WHEN s.token_created_at IS NULL THEN 'B created_at NULL'
         ELSE 'C created_at present'
       END cause, COUNT(*) rows_
  FROM p LEFT JOIN token_static s
       ON lower(s.token_address) = lower(p.token_address) AND s.network_id = p.network_id
 WHERE p.token_age_h IS NULL
 GROUP BY 1 ORDER BY rows_ DESC""")

# 4) training_rows attributable to the 12 NULL-created_at coins
q("4 training_rows joined to the 12 coins with NULL token_created_at", f"""
WITH bad AS (SELECT token_address, network_id FROM token_static WHERE token_created_at IS NULL)
SELECT (SELECT COUNT(*) FROM training_rows t JOIN bad b
          ON b.token_address=t.token_address AND b.network_id=t.network_id) all_rows,
       (SELECT COUNT(*) FROM training_rows t JOIN bad b
          ON b.token_address=t.token_address AND b.network_id=t.network_id
         WHERE {POP}) model_rows,
       (SELECT COUNT(*) FROM training_rows t JOIN bad b
          ON b.token_address=t.token_address AND b.network_id=t.network_id
         WHERE {POP} AND t.token_age_h IS NOT NULL) model_rows_with_age""")

q("4b per-coin breakdown of those training rows", f"""
WITH bad AS (SELECT token_address, network_id, symbol FROM token_static WHERE token_created_at IS NULL)
SELECT b.symbol, b.network_id, COUNT(*) all_rows,
       SUM(CASE WHEN t.kind='signal' THEN 1 ELSE 0 END) signal_rows,
       SUM(CASE WHEN t.kind='signal' AND t.is_live=1 THEN 1 ELSE 0 END) live_signal_rows,
       SUM(CASE WHEN t.feature_version={FV} AND t.kind='signal' AND t.is_live=1
                     AND t.asset_class='meme' AND t.status='ok' AND t.is_independent=1
                THEN 1 ELSE 0 END) model_rows
  FROM training_rows t JOIN bad b
    ON b.token_address=t.token_address AND b.network_id=t.network_id
 GROUP BY 1,2 ORDER BY all_rows DESC""")

con.close()
