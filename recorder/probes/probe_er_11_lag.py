"""fv split for frozen networks + point-in-time lag for static/contract sources. Read-only."""
import os, sqlite3
import config
from probe_er_fam import POP

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== 4663 / 8453 signal training_rows: feature_version x entry day ===")
for net in ("4663", "8453"):
    print(" net", net)
    for r in con.execute("""SELECT feature_version fv, date(entry_ts,'unixepoch') d, COUNT(*) n
        FROM training_rows WHERE kind='signal' AND network_id=?
          AND entry_ts >= strftime('%s','2026-08-01') GROUP BY 1,2 ORDER BY 2,1""", (net,)):
        print(f"   fv={r['fv']} {r['d']} n={r['n']}")
print()

print("=== token_static-empty population rows: lag to the FIRST static row after entry_ts ===")
q = """SELECT COUNT(*) n,
  AVG(lag_min) avg_lag, MIN(lag_min) mn, MAX(lag_min) mx
 FROM (
  SELECT (SELECT MIN(CAST(strftime('%s',s.recorded_at) AS INTEGER)) FROM token_static s
            WHERE s.token_address=t.token_address) - t.entry_ts AS lag_s,
         ((SELECT MIN(CAST(strftime('%s',s.recorded_at) AS INTEGER)) FROM token_static s
            WHERE s.token_address=t.token_address) - t.entry_ts)/60.0 AS lag_min
    FROM training_rows t
   WHERE """ + POP + """ AND decimals IS NULL)"""
r = con.execute(q).fetchone()
print("  ", dict(r))
print()
print("=== distribution of that lag in buckets (minutes) ===")
q = """SELECT bucket, COUNT(*) n FROM (
  SELECT CASE
     WHEN lag_min IS NULL THEN 'no static row ever'
     WHEN lag_min < 0 THEN 'negative(!)'
     WHEN lag_min < 5 THEN '0-5m'
     WHEN lag_min < 30 THEN '5-30m'
     WHEN lag_min < 120 THEN '30m-2h'
     WHEN lag_min < 1440 THEN '2h-24h'
     ELSE '>24h' END bucket
   FROM (SELECT ((SELECT MIN(CAST(strftime('%s',s.recorded_at) AS INTEGER)) FROM token_static s
            WHERE s.token_address=t.token_address) - t.entry_ts)/60.0 AS lag_min
         FROM training_rows t WHERE """ + POP + """ AND decimals IS NULL)
) GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q):
    print(f"   {r['bucket']:22s} {r['n']}")
print()

print("=== same lag for market_snapshot-empty rows (market_ticks) ===")
q = """SELECT bucket, COUNT(*) n FROM (
  SELECT CASE
     WHEN lag_min IS NULL THEN 'no tick row ever'
     WHEN lag_min < 5 THEN '0-5m'
     WHEN lag_min < 30 THEN '5-30m'
     WHEN lag_min < 120 THEN '30m-2h'
     WHEN lag_min < 1440 THEN '2h-24h'
     ELSE '>24h' END bucket
   FROM (SELECT ((SELECT MIN(CAST(strftime('%s',m.recorded_at) AS INTEGER)) FROM market_ticks m
            WHERE m.token_address=t.token_address) - t.entry_ts)/60.0 AS lag_min
         FROM training_rows t WHERE """ + POP + """ AND tick_age_min IS NULL)
) GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q):
    print(f"   {r['bucket']:22s} {r['n']}")
print()

print("=== Base(8453) rows in the WHOLE training_rows vs evm_contract first row lag ===")
q = """SELECT bucket, COUNT(*) n FROM (
  SELECT CASE WHEN lag_min IS NULL THEN 'no contract row ever'
     WHEN lag_min < 0 THEN 'before entry (usable!)'
     WHEN lag_min < 60 THEN '0-60m after'
     WHEN lag_min < 1440 THEN '1-24h after'
     ELSE '>24h after' END bucket
  FROM (SELECT ((SELECT MIN(CAST(strftime('%s',c.recorded_at) AS INTEGER)) FROM evm_contract c
          WHERE c.token_address=t.token_address AND c.network_id=t.network_id) - t.entry_ts)/60.0 lag_min
        FROM training_rows t WHERE kind='signal' AND network_id='8453')
) GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q):
    print(f"   {r['bucket']:24s} {r['n']}")
print()
print("=== evm_contract rows whose recorded_at precedes ANY 8453 signal for the same token ===")
r = con.execute("""SELECT COUNT(*) n FROM evm_contract c
  WHERE EXISTS (SELECT 1 FROM training_rows t WHERE t.kind='signal' AND t.network_id=c.network_id
        AND t.token_address=c.token_address
        AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= t.entry_ts)""").fetchone()
print("   ", r["n"])
