import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

print("features.FEATURE_VERSION (code) =", features.FEATURE_VERSION)

print("\n=== A: meta flags that gate the EVM training rebuild ===")
for r in q("""SELECT key, value FROM meta WHERE key IN
             ('current_feature_version','evm_ledger_rebuild_required',
              'evm_training_rebuild_started','evm_ledger_generation',
              'evm_ledger_rebuild_cohort')"""):
    v = r["value"]
    print(f"   {r['key']:34s} = {v[:120] if isinstance(v,str) else v}")

print("\n=== B: single-pass pivot over the WHOLE table (my own shape) ===")
r = q("""SELECT COUNT(*) total,
                SUM(feature_version=12) fv12,
                SUM(feature_version=8)  fv8,
                SUM(feature_version NOT IN (8,12)) fvother,
                COUNT(DISTINCT feature_version) nvers
         FROM training_rows""")[0]
print(dict(r))
print(f"   stale(!=12) share of whole table = "
      f"{100.0*(r['total']-r['fv12'])/r['total']:.2f}%")

print("\n=== C: stale rows by kind x network (mine) ===")
for r in q("""SELECT kind, COALESCE(network_id,'<null>') net, COUNT(*) n
              FROM training_rows WHERE feature_version<>12
              GROUP BY 1,2 ORDER BY n DESC"""):
    print(f"   {r['kind']:9s} net={r['net']:12s} n={r['n']}")

print("\n=== D: THE population that actually matters — model population ===")
FV = ("CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0')"
      " AS INTEGER)")
BASE = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
        "AND is_independent=1")
r = q(f"""SELECT COUNT(*) eligible_ignoring_fv,
                 SUM(feature_version = {FV}) in_model,
                 SUM(feature_version <> {FV}) blocked_only_by_fv
          FROM training_rows WHERE {BASE}""")[0]
print(dict(r))
if r["eligible_ignoring_fv"]:
    print(f"   blocked-by-fv share of model-eligible = "
          f"{100.0*r['blocked_only_by_fv']/r['eligible_ignoring_fv']:.2f}%")
    print(f"   would-be growth of model population = "
          f"+{100.0*r['blocked_only_by_fv']/max(1,r['in_model']):.1f}%")

print("\n=== E: of the 'stale' rows, how many could EVER enter the model? ===")
for r in q("""SELECT feature_version fv, kind,
                     COALESCE(SUM(is_live=1),0) live,
                     COALESCE(SUM(is_live=0 OR is_live IS NULL),0) notlive,
                     COALESCE(SUM(asset_class='meme'),0) meme,
                     COALESCE(SUM(status='ok'),0) ok,
                     COALESCE(SUM(is_independent=1),0) indep,
                     COALESCE(SUM(kind='signal' AND is_live=1 AND asset_class='meme'
                         AND status='ok' AND is_independent=1),0) model_shaped,
                     COUNT(*) n
              FROM training_rows WHERE feature_version<>12
              GROUP BY 1,2 ORDER BY n DESC"""):
    print(f"   fv={r['fv']} {r['kind']:9s} n={r['n']:6d} live={r['live']:6d} "
          f"meme={r['meme']:6d} ok={r['ok']:6d} indep={r['indep']:6d} "
          f"model_shaped={r['model_shaped']:6d}")

print("\n=== F: is any (kind,key) duplicated across feature versions? ===")
print(q("""SELECT COUNT(*) c FROM (SELECT kind, key FROM training_rows
           GROUP BY 1,2 HAVING COUNT(DISTINCT feature_version)>1)""")[0]["c"])
print("   training_rows PK/indexes:")
for r in q("SELECT name, sql FROM sqlite_master WHERE tbl_name='training_rows'"):
    print("   ", r["name"], "|", (r["sql"] or "").replace("\n", " ")[:200])

print("\n=== G: build recency — built_at extremes per fv (is the builder alive?) ===")
for r in q("""SELECT feature_version fv, COUNT(*) n,
                     MIN(built_at) min_built, MAX(built_at) max_built,
                     MIN(datetime(entry_ts,'unixepoch')) min_entry,
                     MAX(datetime(entry_ts,'unixepoch')) max_entry
              FROM training_rows GROUP BY 1 ORDER BY 1"""):
    print(f"   fv={r['fv']:3d} n={r['n']:7d} built {r['min_built']} .. {r['max_built']}"
          f"  entry {r['min_entry']} .. {r['max_entry']}")

print("\n=== H: per-network fv split for signal rows (entry_ts extremes) ===")
for r in q("""SELECT COALESCE(network_id,'<null>') net, feature_version fv, COUNT(*) n,
                     MIN(datetime(entry_ts,'unixepoch')) e0,
                     MAX(datetime(entry_ts,'unixepoch')) e1,
                     MAX(built_at) b1
              FROM training_rows WHERE kind='signal'
              GROUP BY 1,2 ORDER BY 1,2"""):
    print(f"   net={r['net']:12s} fv={r['fv']:3d} n={r['n']:6d} "
          f"entry {r['e0']} .. {r['e1']}  last_built {r['b1']}")

print("\n=== I: pending outcomes the builder still owes at fv=12 (no EVM filter) ===")
r = q("""SELECT COUNT(*) n FROM outcomes o
         WHERE o.status IN ('ok','no_bars')
           AND NOT EXISTS (SELECT 1 FROM training_rows r
                WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)""")[0]
print("   outcomes with no fv=12 row at all:", r["n"])
for r in q("""SELECT COALESCE(o.network_id,'<null>') net, o.kind, COUNT(*) n
              FROM outcomes o
              WHERE o.status IN ('ok','no_bars')
                AND NOT EXISTS (SELECT 1 FROM training_rows r
                     WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
              GROUP BY 1,2 ORDER BY n DESC"""):
    print(f"   net={r['net']:12s} {r['kind']:9s} n={r['n']}")

print("\n=== J: replay/ledger pending work (why the gate is still closed) ===")
for t in ("evm_replay_state", "evm_backfill_state"):
    try:
        cols = [c["name"] for c in q(f"PRAGMA table_info({t})")]
        print(f"   {t} cols: {cols}")
        for r in q(f"SELECT * FROM {t} LIMIT 12"):
            print("     ", dict(r))
    except sqlite3.Error as e:
        print(f"   {t}: {e}")

print("\n=== K: PRAGMA table_info(training_rows) vs features.ROW_COLUMNS ===")
live = [c["name"] for c in q("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)
print("   live cols:", len(live), " ROW_COLUMNS:", len(rc))
print("   in table but NOT in ROW_COLUMNS:", sorted(set(live) - set(rc)))
print("   in ROW_COLUMNS but NOT in table:", sorted(set(rc) - set(live)))
con.close()
