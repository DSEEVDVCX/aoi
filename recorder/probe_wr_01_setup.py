import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

cols = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)
print("live table columns:", len(cols))
print("ROW_COLUMNS:", len(rc))
print("in table but NOT in ROW_COLUMNS:", sorted(set(cols) - set(rc)))
print("in ROW_COLUMNS but NOT in table:", sorted(set(rc) - set(cols)))

fv = con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()
print("current_feature_version:", fv["value"] if fv else None)

POP = """FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
 AND status='ok' AND is_independent=1
 AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
n = con.execute("SELECT COUNT(*) " + POP).fetchone()[0]
print("model population rows:", n)
print("total training_rows:", con.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0])
for r in con.execute("SELECT kind, is_live, feature_version, COUNT(*) n FROM training_rows GROUP BY 1,2,3 ORDER BY n DESC LIMIT 20"):
    print(dict(r))
print("---- entry_ts range in pop ----")
r = con.execute("SELECT MIN(entry_ts) a, MAX(entry_ts) b, MIN(built_at) c, MAX(built_at) d " + POP).fetchone()
print(dict(r))
con.close()
