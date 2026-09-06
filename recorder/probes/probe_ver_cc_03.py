import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
for r in con.execute("SELECT key, value FROM meta WHERE key LIKE '%rebuild%' OR key LIKE '%replay%' OR key LIKE '%ledger%' ORDER BY key"):
    v = r["value"]
    print(f"{r['key']:<48} {str(v)[:90]}")
