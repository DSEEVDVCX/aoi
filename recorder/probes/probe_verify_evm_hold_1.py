import os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("features.FEATURE_VERSION =", features.FEATURE_VERSION)
print("EVM_NETWORKS =", config.EVM_NETWORKS, "EVM_REPLAY_NETWORKS =", config.EVM_REPLAY_NETWORKS)

print("\n--- meta keys of interest ---")
for r in con.execute("""SELECT key, value FROM meta WHERE key LIKE '%evm%' OR key LIKE '%feature_version%'
                        OR key LIKE '%cohort%' OR key LIKE '%rebuild%' ORDER BY key"""):
    v = r["value"]
    print(f"  {r['key']:<45} = {str(v)[:160]}")

print("\n--- meta table_info ---")
print([c[1] for c in con.execute("PRAGMA table_info(meta)")])

print("\n--- distinct feature_version in training_rows (whole table) ---")
for r in con.execute("SELECT feature_version fv, COUNT(*) n, MIN(entry_ts) mn, MAX(entry_ts) mx FROM training_rows GROUP BY 1 ORDER BY 1"):
    print(f"  fv={r['fv']}  n={r['n']:>7}  {r['mn']} .. {r['mx']}")
con.close()
