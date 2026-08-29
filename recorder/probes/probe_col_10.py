import os, sqlite3, config, datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

NOW = datetime.datetime.now(datetime.timezone.utc)
print("NOW:", NOW.isoformat())
print()
rows = con.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
print("total meta keys:", len(rows))
for r in rows:
    v = r["value"]
    if v is not None and len(str(v)) > 160:
        v = str(v)[:160] + "…"
    print(f"{r['key']:52s} = {v}")
