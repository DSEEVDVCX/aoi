"""List tables + row counts, and meta keys, read-only."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== TABLES ===")
for r in con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name"):
    n = "?"
    if r["type"] == "table":
        try:
            n = con.execute(f"SELECT COUNT(*) c FROM \"{r['name']}\"").fetchone()["c"]
        except Exception as e:
            n = f"ERR {e}"
    print(f"  {r['type']:5s} {r['name']:38s} {n}")

print()
print("=== META keys (name only) ===")
for r in con.execute("SELECT key, substr(CAST(value AS TEXT),1,60) v FROM meta ORDER BY key"):
    print(f"  {r['key']:44s} {r['v']}")
