import os, sqlite3, config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

live = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)").fetchall()]
code = list(features.ROW_COLUMNS)
print("live cols:", len(live), "ROW_COLUMNS:", len(code))
extra_live = [c for c in live if c not in code]
missing_live = [c for c in code if c not in live]
print("in LIVE table but NOT in features.ROW_COLUMNS:", extra_live)
print("in ROW_COLUMNS but NOT in live table:", missing_live)

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
          AND status='ok' AND is_independent=1 AND feature_version = {FV}"""
n = con.execute(f"SELECT COUNT(*) {POP}").fetchone()[0]
print("model population:", n)
print("current_feature_version:",
      con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

# if any extra live column exists, measure its NULL rate in the population
for c in extra_live:
    r = con.execute(f"SELECT COUNT(*) t, SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) nn {POP}").fetchone()
    print(f"  extra col {c}: {r['nn']}/{r['t']} NULL")
con.close()
