import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, p=()):
    return con.execute(sql, p).fetchall()

print("=== meta: fv + evm rebuild gate ===")
for r in q("""SELECT key, value FROM meta
               WHERE key IN ('current_feature_version',
                             'evm_ledger_rebuild_required',
                             'evm_training_rebuild_started')
                  OR key LIKE 'evm_%rebuild%' OR key LIKE '%buildrows%'
                  OR key LIKE '%build_rows%'
               ORDER BY key"""):
    print(f"   {r['key']:44s} = {r['value']}")

print("\n=== PK of training_rows (are fv=8 rows duplicates or the only copy?) ===")
print("   PK cols:", [r["name"] for r in q("PRAGMA table_info(training_rows)") if r["pk"]])
print("   total rows              :", q("SELECT COUNT(*) c FROM training_rows")[0]["c"])
print("   distinct (kind,key)     :",
      q("SELECT COUNT(*) c FROM (SELECT DISTINCT kind,key FROM training_rows)")[0]["c"])
print("   (kind,key) with >1 fv   :",
      q("""SELECT COUNT(*) c FROM (SELECT kind,key FROM training_rows
             GROUP BY 1,2 HAVING COUNT(DISTINCT feature_version)>1)""")[0]["c"])

print("\n=== my own pivot: fv distribution as a single CASE aggregate ===")
r = q("""SELECT COUNT(*) total,
                SUM(CASE WHEN feature_version=12 THEN 1 ELSE 0 END) fv12,
                SUM(CASE WHEN feature_version=8  THEN 1 ELSE 0 END) fv8,
                SUM(CASE WHEN feature_version NOT IN (8,12) THEN 1 ELSE 0 END) fvother,
                MIN(feature_version) mn, MAX(feature_version) mx
           FROM training_rows""")[0]
print(dict(r))
print(f"   stale pct = {100.0*r['fv8']/r['total']:.4f}%")

print("\n=== fv=8 rows: does the MODEL population even want them? ===")
print("   (model population = kind=signal AND is_live=1 AND asset_class=meme")
print("    AND status=ok AND is_independent=1)")
r = q("""SELECT COUNT(*) fv8_all,
                SUM(CASE WHEN kind='signal' THEN 1 ELSE 0 END) sig,
                SUM(CASE WHEN kind='signal' AND is_live=1 THEN 1 ELSE 0 END) sig_live,
                SUM(CASE WHEN kind='signal' AND is_live=1 AND asset_class='meme'
                         THEN 1 ELSE 0 END) sig_live_meme,
                SUM(CASE WHEN kind='signal' AND is_live=1 AND asset_class='meme'
                          AND status='ok' THEN 1 ELSE 0 END) plus_ok,
                SUM(CASE WHEN kind='signal' AND is_live=1 AND asset_class='meme'
                          AND status='ok' AND is_independent=1
                         THEN 1 ELSE 0 END) full_model_pop
           FROM training_rows WHERE feature_version=8""")[0]
print("  ", dict(r))

print("\n=== size of the CURRENT model population (fv=12) for the denominator ===")
r = q("""SELECT COUNT(*) c FROM training_rows
          WHERE kind='signal' AND is_live=1 AND asset_class='meme'
            AND status='ok' AND is_independent=1
            AND feature_version = CAST(COALESCE(
                (SELECT value FROM meta WHERE key='current_feature_version'),'0')
                AS INTEGER)""")[0]
print("   model population (base-table proxy):", r["c"])

print("\n=== fv=8 by network, and is any NON-EVM network stale? ===")
for r in q("""SELECT COALESCE(network_id,'<null>') net, kind, COUNT(*) n,
                     SUM(CASE WHEN is_live=1 THEN 1 ELSE 0 END) live
                FROM training_rows WHERE feature_version=8
               GROUP BY 1,2 ORDER BY n DESC"""):
    print(f"   {r['net']:>12s} {r['kind']:8s} n={r['n']:6d} live={r['live']:6d}")

print("\n=== per-network fv split (is Solana fully upgraded?) ===")
for r in q("""SELECT COALESCE(network_id,'<null>') net,
                     SUM(CASE WHEN feature_version=12 THEN 1 ELSE 0 END) fv12,
                     SUM(CASE WHEN feature_version=8 THEN 1 ELSE 0 END) fv8,
                     COUNT(*) tot
                FROM training_rows GROUP BY 1 ORDER BY tot DESC"""):
    pct = 100.0*r["fv8"]/r["tot"] if r["tot"] else 0
    print(f"   {r['net']:>12s} fv12={r['fv12']:6d} fv8={r['fv8']:6d} tot={r['tot']:6d} stale={pct:5.1f}%")
con.close()
