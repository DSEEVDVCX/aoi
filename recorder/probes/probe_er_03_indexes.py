"""Indexes + address case profile per enrichment table (read-only, cheap)."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== indexes ===")
for r in con.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name"):
    print(f"  {r['tbl_name']:24s} {r['name']:40s} {(r['sql'] or '(auto)')[:90]}")
