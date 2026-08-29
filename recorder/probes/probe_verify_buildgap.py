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

names = [r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("outcome-ish tables:", [n for n in names if "outcome" in n or "label" in n])
print()
print("outcomes cols:", [r["name"] for r in con.execute("PRAGMA table_info(outcomes)")])
print()

q("O1. outcomes by network: count + newest entry",
  "SELECT o.network_id, COUNT(*) n, o.status, MAX(o.entry_ts) mx, "
  "datetime(MAX(o.entry_ts),'unixepoch') newest FROM outcomes o GROUP BY 1,3 ORDER BY 1,2 DESC")

q("O2. Base + RH outcomes recency vs Solana/BSC (status ok only)",
  "SELECT network_id, COUNT(*) n, datetime(MAX(entry_ts),'unixepoch') newest_ok "
  "FROM outcomes WHERE status='ok' GROUP BY 1")

q("O3. training_rows status mix per network (all kinds)",
  "SELECT network_id, status, COUNT(*) n, datetime(MAX(entry_ts),'unixepoch') newest "
  "FROM training_rows GROUP BY 1,2 ORDER BY 1, 3 DESC")

q("O4. build_at recency: when were the newest Base rows physically written?",
  "SELECT network_id, COUNT(*) n, MAX(built_at) newest_built FROM training_rows GROUP BY 1")

con.close()
