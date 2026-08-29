import os, sqlite3, json, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

out = {}

rows = con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").fetchall()
out["objects"] = [(r["name"], r["type"]) for r in rows]

counts = {}
for r in rows:
    if r["type"] != "table":
        continue
    n = r["name"]
    if n.startswith("sqlite_"):
        continue
    try:
        counts[n] = con.execute(f"SELECT COUNT(*) FROM \"{n}\"").fetchone()[0]
    except Exception as e:
        counts[n] = f"ERR {e}"
out["counts"] = counts

# live training_rows columns vs features.ROW_COLUMNS
import features
live_cols = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)").fetchall()]
rc = list(features.ROW_COLUMNS)
out["training_rows_live_col_count"] = len(live_cols)
out["ROW_COLUMNS_count"] = len(rc)
out["in_live_not_in_ROW_COLUMNS"] = [c for c in live_cols if c not in rc]
out["in_ROW_COLUMNS_not_in_live"] = [c for c in rc if c not in live_cols]

# meta keys of interest
meta = {}
for k in ("current_feature_version", "raw_encoding", "schema_version"):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    meta[k] = r[0] if r else None
out["meta"] = meta

print(json.dumps(out, indent=1, default=str))
