import os, sqlite3, sys, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("== TABLES ==")
for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(r["name"])
print()
print("== VIEWS ==")
for r in con.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"):
    print(r["name"])
