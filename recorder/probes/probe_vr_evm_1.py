import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(sql, p=()):
    return con.execute(sql, p).fetchall()

print("=== meta ===")
for r in q("""SELECT key,value FROM meta WHERE key LIKE '%evm%' OR key='current_feature_version'
              ORDER BY key"""):
    v = r["value"]
    print(f"  {r['key']:<42} = {str(v)[:120]}")

print("\n=== config ===")
print("  EVM_NETWORKS       =", config.EVM_NETWORKS)
print("  EVM_REPLAY_NETWORKS=", config.EVM_REPLAY_NETWORKS)
excluded = sorted({*(str(n) for n in config.EVM_NETWORKS),
                   *(str(n) for n in config.EVM_REPLAY_NETWORKS)})
print("  EXCLUDED SET       =", excluded)
import features
print("  features.FEATURE_VERSION =", features.FEATURE_VERSION)
print("  LIVE_START_TS =", config.LIVE_START_TS)

print("\n=== training_rows: model-filter population minus fv, by fv x network x kind ===")
for r in q("""SELECT feature_version AS fv, COALESCE(network_id,'(null)') AS net,
                     COUNT(*) AS n, MIN(built_at) AS first_built, MAX(built_at) AS last_built
              FROM training_rows
              WHERE kind='signal' AND is_live=1 AND asset_class='meme'
                AND status='ok' AND is_independent=1
              GROUP BY 1,2 ORDER BY 1,3 DESC"""):
    print(f"  fv={r['fv']:<3} net={r['net']:<12} n={r['n']:<6} built {str(r['first_built'])[:19]} .. {str(r['last_built'])[:19]}")
con.close()
