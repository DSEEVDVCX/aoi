import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import features  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

cur = con.execute("PRAGMA table_info(training_rows)")
live_cols = [r["name"] for r in cur.fetchall()]
row_cols = list(features.ROW_COLUMNS)

print("live table cols:", len(live_cols))
print("features.ROW_COLUMNS:", len(row_cols))
print("IN LIVE TABLE, NOT IN ROW_COLUMNS:", sorted(set(live_cols) - set(row_cols)))
print("IN ROW_COLUMNS, NOT IN LIVE TABLE:", sorted(set(row_cols) - set(live_cols)))

fv = con.execute(
    "SELECT value FROM meta WHERE key='current_feature_version'"
).fetchone()
print("current_feature_version:", fv["value"] if fv else None)

pop_sql = """
SELECT COUNT(*) AS n FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE(
       (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
"""
print("model population rows:", con.execute(pop_sql).fetchone()["n"])

print("total training_rows:", con.execute("SELECT COUNT(*) AS n FROM training_rows").fetchone()["n"])

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_ef_cols.json"), "w") as fh:
    json.dump({"live": live_cols, "row_columns": row_cols}, fh, indent=1)
con.close()
