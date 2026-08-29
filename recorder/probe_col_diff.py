"""Column diff: PRAGMA table_info(training_rows) vs features.ROW_COLUMNS."""
import os, sqlite3, json
import config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

tbl = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
rc = list(features.ROW_COLUMNS)

print("table cols :", len(tbl))
print("ROW_COLUMNS:", len(rc))
print()
print("IN TABLE, NOT IN ROW_COLUMNS (builder never writes them):")
for c in tbl:
    if c not in rc:
        print("   ", c)
print()
print("IN ROW_COLUMNS, NOT IN TABLE:")
for c in rc:
    if c not in tbl:
        print("   ", c)
print()
print("META:", len(features.META_COLUMNS), "FEATURE:", len(features.FEATURE_COLUMNS), "LABEL:", len(features.LABEL_COLUMNS))
print()
cfv = con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()
print("current_feature_version:", cfv["value"] if cfv else None)
print()
print("counts by kind/feature_version:")
for r in con.execute("SELECT kind, feature_version, COUNT(*) n FROM training_rows GROUP BY 1,2 ORDER BY 1,2"):
    print(f"   {r['kind']:10s} fv={r['feature_version']} n={r['n']}")
print()
print("model population count:")
q = """SELECT COUNT(*) n FROM training_rows
WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
  AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
print("   ", con.execute(q).fetchone()["n"])
print()
print("total training_rows:", con.execute("SELECT COUNT(*) n FROM training_rows").fetchone()["n"])

with open("probe_cols.json", "w", encoding="utf-8") as f:
    json.dump({"table": tbl, "row_columns": rc}, f)
print("wrote probe_cols.json")
