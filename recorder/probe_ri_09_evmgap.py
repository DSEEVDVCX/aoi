import os, sqlite3, config, time

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = int(con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

def q(label, sql, args=()):
    t0 = time.time()
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"{label}: FAILED {e}")
        return None
    print(f"--- {label}  ({time.time()-t0:.1f}s)")
    for r in rows[:40]:
        print("   ", dict(r))
    if len(rows) > 40:
        print(f"    ... {len(rows)} rows total")
    return rows

print("EVM_NETWORKS:", getattr(config,'EVM_NETWORKS',None))
print("EVM_REPLAY_NETWORKS:", getattr(config,'EVM_REPLAY_NETWORKS',None))
print("LIVE_START_TS:", getattr(config,'LIVE_START_TS',None))
print("MIN_TOKEN_AGE_DAYS:", getattr(config,'MIN_TOKEN_AGE_DAYS',None))

q("relevant meta keys", """
SELECT key, substr(CAST(value AS TEXT),1,80) v FROM meta
 WHERE key LIKE '%rebuild%' OR key LIKE '%feature%' OR key LIKE '%buildrows%'
    OR key LIKE '%labeler%' OR key LIKE '%last_run%' OR key LIKE '%evm_ledger%'
 ORDER BY key""")

print()
print("=== outcomes vs training_rows per network (status ok/no_bars = buildable) ===")
q("buildable outcomes vs built rows per network+kind", f"""
SELECT o.network_id, o.kind,
       COUNT(*) buildable_outcomes,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key) THEN 1 ELSE 0 END) has_any_row,
       SUM(CASE WHEN EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version={FV}) THEN 1 ELSE 0 END) has_current_fv
  FROM outcomes o
 WHERE o.status IN ('ok','no_bars')
 GROUP BY 1,2 ORDER BY 1,2""")

print()
print("=== training_rows feature_version per network ===")
q("fv per network", "SELECT network_id, feature_version, COUNT(*) n FROM training_rows GROUP BY 1,2 ORDER BY 1,2")

print()
print("=== the 1699 eligible-but-stale rows ===")
q("eligible signal rows by feature_version", f"""
SELECT feature_version, network_id, COUNT(*) n, MIN(built_at) mn, MAX(built_at) mx
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
 GROUP BY 1,2 ORDER BY 1,2""")

print()
print("=== outcomes that ARE model-eligible but have no current-fv training row ===")
q("model-eligible outcomes missing current-fv training row, per network", f"""
SELECT o.network_id, COUNT(*) n
  FROM outcomes o
 WHERE o.kind='signal' AND o.is_independent=1 AND o.status='ok'
   AND o.entry_ts >= {int(config.LIVE_START_TS)}
   AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind='signal' AND r.key=o.key AND r.feature_version={FV})
 GROUP BY 1 ORDER BY 2 DESC""")

con.close()
