import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, args=()):
    print("=" * 70)
    print(label)
    print("SQL:", " ".join(sql.split()))
    try:
        for r in con.execute(sql, args).fetchall()[:40]:
            print("   ", dict(r))
    except Exception as e:
        print("    FAILED:", e)
    print()

print("signal_events cols:", [r["name"] for r in con.execute("PRAGMA table_info(signal_events)")])
print()
print("evm_contract cols:", [r["name"] for r in con.execute("PRAGMA table_info(evm_contract)")])
print()
print("watch_windows cols:", [r["name"] for r in con.execute("PRAGMA table_info(watch_windows)")])
print()

con.close()
