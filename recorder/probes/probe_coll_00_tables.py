import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("DB:", config.DB_PATH)
rows = con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name").fetchall()
for r in rows:
    print(f"{r['type']:5s} {r['name']}")

print("\n--- row counts ---")
for r in rows:
    if r["type"] != "table":
        continue
    n = r["name"]
    if n.startswith("sqlite_"):
        continue
    try:
        c = con.execute(f"SELECT COUNT(*) FROM \"{n}\"").fetchone()[0]
        print(f"{c:>12,}  {n}")
    except Exception as e:
        print(f"{'ERR':>12}  {n}  {e}")
