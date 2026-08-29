import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

live = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)
print("live table cols :", len(live))
print("ROW_COLUMNS     :", len(rc))
print("in table, NOT in ROW_COLUMNS (would be NULL forever):")
for c in live:
    if c not in rc:
        print("   ", c)
print("in ROW_COLUMNS, NOT in table:")
for c in rc:
    if c not in live:
        print("   ", c)

fc = list(getattr(features, "FEATURE_COLUMNS", []))
print("FEATURE_COLUMNS :", len(fc))
print("dupes in ROW_COLUMNS:", [c for c in set(rc) if rc.count(c) > 1])
con.close()
