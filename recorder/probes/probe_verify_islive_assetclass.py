import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
L = config.LIVE_START_TS
print("LIVE_START_TS from config =", L)

print("\n=== Q1: 2x2 cross-tab is_live x temporal, per kind (my own formulation) ===")
q1 = """
SELECT kind,
       CASE WHEN entry_ts >= :L THEN 'late' ELSE 'early' END AS temporal,
       is_live,
       COUNT(*) AS n,
       MIN(entry_ts) AS min_ts, MAX(entry_ts) AS max_ts
  FROM training_rows
 GROUP BY kind, temporal, is_live
 ORDER BY kind, temporal, is_live
"""
for r in con.execute(q1, {"L": L}):
    print(f"  {r['kind']:<9} {r['temporal']:<6} is_live={r['is_live']}  n={r['n']:<7} "
          f"min={r['min_ts']} max={r['max_ts']}")

print("\n=== Q2: asset_class distribution per kind (NULL vs values) ===")
q2 = """
SELECT kind, COALESCE(asset_class,'<NULL>') AS ac, COUNT(*) n
  FROM training_rows GROUP BY kind, ac ORDER BY kind, n DESC
"""
for r in con.execute(q2):
    print(f"  {r['kind']:<9} {r['ac']:<8} {r['n']}")

print("\n=== Q3: watch rows by outcomes.signal_type + is_control (is the arm retro?) ===")
q3 = """
SELECT o.is_control, o.signal_type, COUNT(*) n,
       SUM(tr.is_live=1) live1, SUM(tr.asset_class IS NULL) ac_null,
       MIN(tr.entry_ts) min_ts
  FROM training_rows tr
  JOIN outcomes o ON o.kind=tr.kind AND o.key=tr.key
 WHERE tr.kind='watch'
 GROUP BY o.is_control, o.signal_type ORDER BY n DESC
"""
for r in con.execute(q3):
    print(f"  is_control={r['is_control']} signal_type={str(r['signal_type']):<16} "
          f"n={r['n']:<7} is_live1={r['live1']} ac_null={r['ac_null']} min_ts={r['min_ts']}")

print("\n=== Q4: activity rows — is the activity feed retro-aggregated? ===")
q4 = """
SELECT COUNT(*) n, MIN(tr.entry_ts) min_ts, MAX(tr.entry_ts) max_ts,
       SUM(tr.is_live=1) live1, SUM(tr.asset_class IS NULL) ac_null
  FROM training_rows tr WHERE tr.kind='activity'
"""
r = con.execute(q4).fetchone()
print(f"  activity n={r['n']} min={r['min_ts']} max={r['max_ts']} "
      f"is_live1={r['live1']} ac_null={r['ac_null']}")

print("\n=== Q5: does model_training_rows' own WHERE already exclude non-signal kinds? ===")
sql = con.execute("SELECT sql FROM sqlite_master WHERE name='model_training_rows'").fetchone()[0]
print("  view WHERE contains kind='signal':", "kind = 'signal'" in sql or "kind='signal'" in sql)

print("\n=== Q6: PRAGMA table_info(training_rows) vs features.ROW_COLUMNS ===")
live_cols = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
rc = set(features.ROW_COLUMNS)
extra = [c for c in live_cols if c not in rc]
missing = [c for c in rc if c not in live_cols]
print(f"  live table cols={len(live_cols)} ROW_COLUMNS={len(rc)}")
print("  in live table but NOT in ROW_COLUMNS:", extra)
print("  in ROW_COLUMNS but NOT in live table:", missing)
for c in extra:
    n = con.execute(f"SELECT COUNT(*) c, SUM({c} IS NULL) nulls FROM training_rows").fetchone()
    print(f"    {c}: {n['nulls']} NULL of {n['c']}")

print("\n=== Q7: their exact SQL, for agreement check ===")
for r in con.execute("""SELECT kind, COUNT(*) rows, SUM(entry_ts >= 1785018927) after_live_start,
                               SUM(is_live=1) marked_live FROM training_rows GROUP BY kind"""):
    print(f"  {r['kind']:<9} rows={r['rows']:<7} after_live={r['after_live_start']:<7} marked_live={r['marked_live']}")

print("\n=== Q8: watch_windows.admission_source for the control arm ===")
q8 = """
SELECT w.is_control, w.admission_source, COUNT(*) n
  FROM watch_windows w
 WHERE w.design_version >= 3
 GROUP BY w.is_control, w.admission_source ORDER BY n DESC LIMIT 20
"""
try:
    for r in con.execute(q8):
        print(f"  is_control={r['is_control']} admission_source={r['admission_source']} n={r['n']}")
except Exception as e:
    print("  FAILED:", e)

print("\n=== Q9: phase1 control path count — does the control arm actually reach analysis? ===")
q9 = """
SELECT o.is_control, COUNT(*) n
  FROM outcomes o
  JOIN watch_windows w
    ON w.token_address || ':' || w.network_id || ':' || w.first_seen_at = o.key
   AND w.design_version >= 3 AND w.admission_source IN ('trending','verified')
  JOIN token_class tc ON tc.token_address=o.token_address AND tc.network_id=o.network_id
   AND tc.asset_class='meme'
  JOIN phase1_watch_outcomes e ON e.kind=o.kind AND e.key=o.key
 WHERE o.kind='watch' AND o.entry_ts >= :L
 GROUP BY o.is_control
"""
try:
    for r in con.execute(q9, {"L": L}):
        print(f"  is_control={r['is_control']} reaches_phase1_analysis n={r['n']}")
except Exception as e:
    print("  FAILED:", e)
con.close()
