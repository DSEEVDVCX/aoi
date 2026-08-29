import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

tabs = [r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
for t in tabs:
    if t == "sqlite_sequence":
        continue
    cols = [(c["name"], c["type"]) for c in con.execute(f"PRAGMA table_info({t})")]
    print(f"--- {t} ---")
    print("   ", ", ".join(f"{n}:{ty}" for n, ty in cols))
