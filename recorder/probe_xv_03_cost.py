"""Adversarial verification, step 3: my OWN measurement of the gated EVM cost.
Uses LEFT JOIN (not NOT EXISTS) and splits the gap into stale-fv vs no-row-at-all.
READ-ONLY."""
import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = 12
LIVE = config.LIVE_START_TS
NAMES = {"1399811149": "solana", "8453": "base", "4663": "robinhood",
         "56": "bsc", "143": "monad", "": "(null)"}

print("== A. model population (base table, the view's cheap filters) by network ==")
sql_a = """
SELECT COALESCE(network_id,'') net, COUNT(*) n,
       date(MIN(entry_ts),'unixepoch') d0, date(MAX(entry_ts),'unixepoch') d1
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE(
       (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
 GROUP BY 1 ORDER BY n DESC"""
tot = 0
for r in con.execute(sql_a):
    tot += r["n"]
    print(f"  {NAMES.get(r['net'], r['net']):<10} {r['net']:<12} {r['n']:>7,}  {r['d0']} .. {r['d1']}")
print(f"  TOTAL model population = {tot:,}")

print("\n== B. EVM signal outcomes without an fv12 row (my own LEFT JOIN) ==")
sql_b = """
SELECT COALESCE(o.network_id,'') net,
       CASE WHEN r.key IS NULL THEN 'no_row_at_all'
            ELSE 'stale_fv' || r.feature_version END bucket,
       COUNT(*) n,
       date(MIN(o.entry_ts),'unixepoch') d0, date(MAX(o.entry_ts),'unixepoch') d1
  FROM outcomes o
  LEFT JOIN training_rows r ON r.kind=o.kind AND r.key=o.key
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
   AND o.entry_ts >= ?
   AND COALESCE(o.network_id,'') IN ('4663','8453','143')
   AND COALESCE(r.feature_version, -1) <> ?
 GROUP BY 1,2 ORDER BY 1,2"""
gtot = 0
for r in con.execute(sql_b, (LIVE, FV)):
    gtot += r["n"]
    print(f"  {NAMES.get(r['net'], r['net']):<10} {r['bucket']:<14} {r['n']:>7,}  {r['d0']} .. {r['d1']}")
print(f"  TOTAL gated (my number) = {gtot:,}")

print("\n== B2. same, per network total only ==")
for r in con.execute("""
SELECT COALESCE(o.network_id,'') net, COUNT(*) n
  FROM outcomes o LEFT JOIN training_rows r ON r.kind=o.kind AND r.key=o.key
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1 AND o.entry_ts >= ?
   AND COALESCE(o.network_id,'') IN ('4663','8453','143')
   AND COALESCE(r.feature_version,-1) <> ? GROUP BY 1 ORDER BY 2 DESC""", (LIVE, FV)):
    print(f"  {NAMES.get(r['net'], r['net']):<10} {r['n']:>7,}")

print("\n== C. of the gated ones, how many are even buildable / meme? ==")
r = con.execute("""
SELECT COUNT(*) total,
       SUM(CASE WHEN se.id IS NULL THEN 1 ELSE 0 END) no_signal_event,
       SUM(CASE WHEN r.key IS NOT NULL AND r.asset_class='meme' THEN 1 ELSE 0 END) stale_meme,
       SUM(CASE WHEN r.key IS NOT NULL AND r.asset_class IS NOT NULL
                     AND r.asset_class<>'meme' THEN 1 ELSE 0 END) stale_not_meme,
       SUM(CASE WHEN r.key IS NOT NULL AND r.asset_class IS NULL THEN 1 ELSE 0 END) stale_null_class,
       SUM(CASE WHEN r.key IS NULL THEN 1 ELSE 0 END) unknown_class,
       SUM(CASE WHEN o.entry_ts > 1787246982 THEN 1 ELSE 0 END) after_cohort_cutoff
  FROM outcomes o
  LEFT JOIN training_rows r ON r.kind=o.kind AND r.key=o.key
  LEFT JOIN signal_events se ON se.id=o.key
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1 AND o.entry_ts >= ?
   AND COALESCE(o.network_id,'') IN ('4663','8453','143')
   AND COALESCE(r.feature_version,-1) <> ?""", (LIVE, FV)).fetchone()
for k in r.keys():
    print(f"  {k:<22} {r[k]:>7,}")

print("\n== D. asset_class mix of ALL live EVM signal rows already built (any fv) ==")
for x in con.execute("""
SELECT COALESCE(asset_class,'(null)') ac, COUNT(*) n FROM training_rows
 WHERE kind='signal' AND is_live=1 AND status='ok' AND is_independent=1
   AND COALESCE(network_id,'') IN ('4663','8453','143') GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  {x['ac']:<10} {x['n']:>7,}")

print("\n== E. when did EVM building stop? max(built_at) per network / fv ==")
for x in con.execute("""
SELECT COALESCE(network_id,'') net, feature_version fv, COUNT(*) n,
       substr(MIN(built_at),1,16) b0, substr(MAX(built_at),1,16) b1
  FROM training_rows WHERE kind='signal' AND is_live=1
 GROUP BY 1,2 ORDER BY 1,2"""):
    print(f"  {NAMES.get(x['net'], x['net']):<10} fv={x['fv']:<3} n={x['n']:>7,}  built {x['b0']} .. {x['b1']}")
con.close()
