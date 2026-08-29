import os, sqlite3, json, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

live_cols = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)
print("live table cols:", len(live_cols), "ROW_COLUMNS:", len(rc))
print("in table NOT in ROW_COLUMNS:", [c for c in live_cols if c not in rc])
print("in ROW_COLUMNS NOT in table:", [c for c in rc if c not in live_cols])

fv = con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
).fetchone()[0]
print("current_feature_version:", fv, "features.FEATURE_VERSION:", features.FEATURE_VERSION)

print("total training_rows:", con.execute("SELECT COUNT(*) FROM training_rows").fetchone()[0])
POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
 AND is_independent=1 AND feature_version=CAST(COALESCE(
 (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
print("model population:", con.execute(f"SELECT COUNT(*) FROM training_rows WHERE {POP}").fetchone()[0])
for q, lbl in [
    ("kind='signal' AND is_live=1", "signal+live"),
    ("kind='signal' AND is_live=1 AND asset_class='meme'", "+meme"),
    ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'", "+ok"),
    ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1", "+indep"),
]:
    print(lbl, con.execute(f"SELECT COUNT(*) FROM training_rows WHERE {q}").fetchone()[0])

print("feature_version histogram (signal+live):")
for r in con.execute(
    "SELECT feature_version, COUNT(*) n FROM training_rows WHERE kind='signal' AND is_live=1 GROUP BY 1 ORDER BY 1"
):
    print("  fv", r[0], r[1])

print("\ntables:")
print([r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
