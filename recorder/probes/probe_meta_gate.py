"""meta flags gating the EVM training rebuild."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row
for r in con.execute("""SELECT key, substr(value,1,120) v FROM meta
                         WHERE key LIKE '%evm%' OR key LIKE '%rebuild%'
                            OR key LIKE '%feature_version%' OR key LIKE '%buildrows%'
                            OR key LIKE '%build_rows%' ORDER BY key"""):
    print(f"   {r['key']:48s} = {r['v']}")
print()
for r in con.execute("SELECT COUNT(*) n FROM meta"):
    print("meta rows:", r["n"])
print("\n-- outcomes by network and status --")
for r in con.execute("""SELECT network_id, status, COUNT(*) n FROM outcomes
                         WHERE kind='signal' GROUP BY 1,2 ORDER BY 1,3 DESC"""):
    print("   ", dict(r))
print("\n-- EVM outcomes eligible but with no fv=12 row --")
q = """SELECT o.network_id, COUNT(*) n, MIN(o.entry_ts) mn, MAX(o.entry_ts) mx
         FROM outcomes o
        WHERE o.status IN ('ok','no_bars')
          AND NOT EXISTS (SELECT 1 FROM training_rows r
                           WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
        GROUP BY 1 ORDER BY n DESC"""
for r in con.execute(q):
    print("   ", dict(r))
