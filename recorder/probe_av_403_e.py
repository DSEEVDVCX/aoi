import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
c = con.cursor()
FVN = 12
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version={FVN}"""

print("=== E1: token_static (token_age_h) NULL rate per day, recent regime ===")
for r in c.execute(f"""
SELECT strftime('%Y-%m-%d', entry_ts,'unixepoch') d, COUNT(*) n,
       SUM(CASE WHEN token_age_h IS NULL THEN 1 ELSE 0 END) nn,
       ROUND(100.0*SUM(CASE WHEN token_age_h IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) pct
  FROM training_rows WHERE {POP} AND entry_ts>=strftime('%s','2026-08-11')
 GROUP BY 1 ORDER BY 4 DESC"""):
    print(f"  {r['d']}  n={r['n']:4d}  token_age_h NULL={r['nn']:3d} ({r['pct']}%)")

print("\n=== E2: token_age_h NULL, WHOLE history (is 08-19 an outlier?) ===")
vals = []
for r in c.execute(f"""
SELECT strftime('%Y-%m-%d', entry_ts,'unixepoch') d, COUNT(*) n,
       ROUND(100.0*SUM(CASE WHEN token_age_h IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) pct
  FROM training_rows WHERE {POP} GROUP BY 1 HAVING COUNT(*)>=100 ORDER BY 1"""):
    vals.append((r['d'], r['n'], r['pct']))
print("  min", min(v[2] for v in vals), "max", max(v[2] for v in vals),
      "mean", round(sum(v[2] for v in vals)/len(vals), 2))
print("  top5:", sorted(vals, key=lambda v: -v[2])[:5])
print("  08-19:", [v for v in vals if v[0] == '2026-08-19'])

print("\n=== E3: distribution of EMPTY UTC hours per day -- is 16 an outlier? ===")
eh = []
for r in c.execute(f"""
SELECT strftime('%Y-%m-%d', entry_ts,'unixepoch') d,
       24 - COUNT(DISTINCT strftime('%H', entry_ts,'unixepoch')) empty_hours, COUNT(*) n
  FROM training_rows WHERE {POP} GROUP BY 1 HAVING COUNT(*)>=100 ORDER BY 2 DESC"""):
    eh.append((r['d'], r['empty_hours'], r['n']))
for d, e, n in eh[:10]:
    print(f"  {d}  empty_hours={e:2d}  n={n}")
print(f"  mean empty_hours over {len(eh)} days = {sum(e for _,e,_ in eh)/len(eh):.1f}; "
      f"median-ish sorted = {sorted(e for _,e,_ in eh)}")

print("\n=== E4: pipeline lag -- feed events with no training row yet (baseline truncation) ===")
for r in c.execute("""
SELECT substr(s.ts,1,10) d, COUNT(*) feed,
       SUM(CASE WHEN t.key IS NULL THEN 1 ELSE 0 END) no_row
  FROM signal_events s
  LEFT JOIN training_rows t ON t.key=s.id AND t.kind='signal'
 WHERE s.ts>='2026-08-18' AND s.ts<'2026-08-22'
 GROUP BY 1 ORDER BY 1"""):
    print(f"  {r['d']}  feed={r['feed']:6d}  without_training_row={r['no_row']:6d}")

print("\n=== E5: PRAGMA table_info(training_rows) vs features.ROW_COLUMNS (drift check) ===")
import features
live = [r['name'] for r in c.execute("PRAGMA table_info(training_rows)")]
meta_cols = {'kind','key','token_address','network_id','entry_ts','asset_class','split',
             'is_independent','is_live','status','suspect_bars','feature_version','built_at'}
extra = [x for x in live if x not in set(features.ROW_COLUMNS) and x not in meta_cols]
missing = [x for x in features.ROW_COLUMNS if x not in set(live)]
print("  live cols:", len(live), " ROW_COLUMNS:", len(features.ROW_COLUMNS))
print("  in live table but NOT in ROW_COLUMNS (would be NULL forever):", extra)
print("  in ROW_COLUMNS but not in live table:", missing)

con.close()
