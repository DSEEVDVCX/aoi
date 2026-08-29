"""Step 6: sanity-check signal_events.ts type before quoting a per-day count. READ-ONLY."""
import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
r = con.execute("SELECT ts, typeof(ts) t FROM signal_events LIMIT 3").fetchall()
for x in r:
    print("  ts =", x["ts"], " typeof =", x["t"])
print()
for x in con.execute("""
SELECT COALESCE(network_id,'') net, COUNT(*) n FROM signal_events
 WHERE substr(ts,1,10)='2026-08-22' AND COALESCE(network_id,'') IN ('4663','8453','143')
 GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net={x['net']:<10} signals on 2026-08-22 = {x['n']:>7,}")
print()
for x in con.execute("""
SELECT COALESCE(network_id,'') net, COUNT(*) n FROM signal_events
 GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""):
    print(f"  net={x['net']:<10} lifetime signal_events = {x['n']:>7,}")
con.close()
