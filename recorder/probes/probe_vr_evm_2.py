import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, p=()):
    return con.execute(sql, p).fetchall()

CUR = int(q("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) AS v")[0]["v"])
print("current_feature_version =", CUR)

print("\n=== A. my own LEFT JOIN version: model-candidate outcomes with no row at current fv ===")
rows = q("""
  SELECT COALESCE(o.network_id,'(null)') AS net, COUNT(*) AS missing
  FROM outcomes o
  LEFT JOIN training_rows r
    ON r.kind = o.kind AND r.key = o.key AND r.feature_version = ?
  WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
    AND o.entry_ts >= ?
    AND r.key IS NULL
  GROUP BY 1 ORDER BY 2 DESC""", (CUR, config.LIVE_START_TS))
tot = 0
for r in rows:
    tot += r["missing"]; print(f"  net={r['net']:<12} missing={r['missing']}")
print("  TOTAL =", tot)

print("\n=== B. same but ANY feature_version row exists? (is it stale-fv or never-built?) ===")
rows = q("""
  SELECT COALESCE(o.network_id,'(null)') AS net,
         SUM(CASE WHEN e.n IS NULL THEN 1 ELSE 0 END) AS never_built,
         SUM(CASE WHEN e.n IS NOT NULL THEN 1 ELSE 0 END) AS stale_only
  FROM outcomes o
  LEFT JOIN training_rows r
    ON r.kind=o.kind AND r.key=o.key AND r.feature_version=?
  LEFT JOIN (SELECT kind,key,COUNT(*) n FROM training_rows GROUP BY 1,2) e
    ON e.kind=o.kind AND e.key=o.key
  WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
    AND o.entry_ts >= ? AND r.key IS NULL
  GROUP BY 1 ORDER BY 2 DESC""", (CUR, config.LIVE_START_TS))
for r in rows:
    print(f"  net={r['net']:<12} never_built={r['never_built']:<6} has_stale_fv_row_only={r['stale_only']}")

print("\n=== C. all labelled outcomes (ok|no_bars) with NO training row at all, by kind x net ===")
rows = q("""
  SELECT o.kind, COALESCE(o.network_id,'(null)') AS net, COUNT(*) AS n
  FROM outcomes o
  WHERE o.status IN ('ok','no_bars')
    AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key)
  GROUP BY 1,2 ORDER BY 3 DESC""")
tot = 0
for r in rows:
    tot += r["n"]; print(f"  kind={r['kind']:<10} net={r['net']:<12} n={r['n']}")
print("  TOTAL =", tot)

print("\n=== D. is the exclusion set the cause? split missing by excluded vs not ===")
EXCL = sorted({*(str(n) for n in config.EVM_NETWORKS), *(str(n) for n in config.EVM_REPLAY_NETWORKS)})
marks = ",".join("?" for _ in EXCL)
r = q(f"""
  SELECT SUM(CASE WHEN COALESCE(o.network_id,'') IN ({marks}) THEN 1 ELSE 0 END) AS in_excl,
         SUM(CASE WHEN COALESCE(o.network_id,'') NOT IN ({marks}) THEN 1 ELSE 0 END) AS not_excl
  FROM outcomes o
  WHERE o.status IN ('ok','no_bars')
    AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key)""",
  EXCL*2)[0]
print(f"  excluded-set nets={r['in_excl']}   NOT-excluded nets={r['not_excl']}   (excl set={EXCL})")

print("\n=== E. labelled live independent signal SUPPLY per net (denominator) ===")
for r in q("""SELECT COALESCE(network_id,'(null)') AS net, COUNT(*) n
              FROM outcomes WHERE kind='signal' AND status='ok' AND is_independent=1
                AND entry_ts>=? GROUP BY 1 ORDER BY 2 DESC""", (config.LIVE_START_TS,)):
    print(f"  net={r['net']:<12} n={r['n']}")
con.close()
