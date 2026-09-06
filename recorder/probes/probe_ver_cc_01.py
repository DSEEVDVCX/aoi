import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("meta current_feature_version:",
      con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

for t in ("training_rows", "chain_concentration"):
    print("---", t)
    for r in con.execute(f"PRAGMA table_info({t})"):
        if r["name"] in ("network_id", "token_address", "entry_ts", "recorded_at",
                         "feature_version", "built_at", "is_live", "key", "kind"):
            print("  ", r["name"], r["type"])

print("\nchain_concentration totals:")
for r in con.execute("""SELECT network_id, is_replay, COUNT(*) n,
                               SUM(top1_pct IS NOT NULL) t1,
                               SUM(holder_count IS NOT NULL) hc,
                               MIN(recorded_at) a, MAX(recorded_at) b
                          FROM chain_concentration
                         GROUP BY network_id, is_replay ORDER BY n DESC"""):
    print("  ", dict(r))
