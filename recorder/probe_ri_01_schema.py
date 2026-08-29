import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("DB:", config.DB_PATH)

# 1) live training_rows columns vs features.ROW_COLUMNS
live = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)
print("live training_rows cols:", len(live), " ROW_COLUMNS:", len(rc))
extra_live = [c for c in live if c not in rc]
extra_rc = [c for c in rc if c not in live]
print("IN LIVE TABLE BUT NOT IN ROW_COLUMNS:", extra_live)
print("IN ROW_COLUMNS BUT NOT IN LIVE TABLE:", extra_rc)

# also check outcomes live cols
for t in ("outcomes", "watch_windows", "watchlist", "token_static", "signal_events"):
    cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})")]
    print(f"\n{t} ({len(cols)}):", cols)

# 2) row counts
print("\n--- counts ---")
for t in ("signal_events","watchlist","watch_windows","token_static","outcomes","training_rows","token_class"):
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"{t:18s} {n:>10,}")

print("\n--- meta ---")
for r in con.execute("SELECT key, substr(CAST(value AS TEXT),1,80) v FROM meta WHERE key LIKE '%feature_version%' OR key LIKE '%live_start%' OR key LIKE '%LIVE%' ORDER BY key"):
    print(f"  {r['key']:40s} = {r['v']}")
con.close()
