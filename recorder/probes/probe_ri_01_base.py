import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("DB:", config.DB_PATH)

tables = ["signal_events","watchlist","watch_windows","token_static","outcomes","training_rows"]
for t in tables:
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"count {t}: {n}")

# feature version
fv = con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()
print("current_feature_version:", fv[0] if fv else None)

# PRAGMA table_info(training_rows) vs features.ROW_COLUMNS
live_cols = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
row_cols = list(features.ROW_COLUMNS)
print("live training_rows cols:", len(live_cols), " ROW_COLUMNS:", len(row_cols))
extra_in_live = [c for c in live_cols if c not in row_cols]
extra_in_code = [c for c in row_cols if c not in live_cols]
print("in live table but NOT in features.ROW_COLUMNS:", extra_in_live)
print("in features.ROW_COLUMNS but NOT in live table:", extra_in_code)

# same for outcomes vs schema? just list
for t in ["outcomes","watch_windows","watchlist","token_static","signal_events"]:
    cols = [r["name"] for r in con.execute(f"PRAGMA table_info({t})")]
    print(f"\n{t} cols ({len(cols)}):", cols)

con.close()
