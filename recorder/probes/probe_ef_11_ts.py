import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=180)
con.row_factory = sqlite3.Row

r = con.execute("SELECT ts, typeof(ts) t, recorded_at FROM signal_events LIMIT 3").fetchall()
for x in r:
    print(dict(x))

print("\n=== signal_events by network, using substr(ts,1,10) as day ===")
for r in con.execute("""
    SELECT network_id, COUNT(*) n, MIN(substr(ts,1,10)) a, MAX(substr(ts,1,10)) b
      FROM signal_events GROUP BY 1 ORDER BY n DESC"""):
    print("  ", dict(r))

print("\n=== signal_events daily count per network, 2026-08-04 .. 2026-08-22 ===")
cur = con.execute("""
    SELECT substr(ts,1,10) d, network_id, COUNT(*) n
      FROM signal_events WHERE substr(ts,1,10) >= '2026-08-03'
     GROUP BY 1,2 ORDER BY 1,2""")
byday = {}
for d, net, n in cur:
    byday.setdefault(d, {})[str(net)] = n
nets = ["1399811149", "56", "4663", "8453", "143"]
print("%-12s %9s %7s %9s %7s %5s" % ("day", "solana", "bsc", "robinhood", "base", "143"))
for d in sorted(byday):
    v = byday[d]
    print("%-12s %9d %7d %9d %7d %5d" % (
        d, v.get("1399811149", 0), v.get("56", 0), v.get("4663", 0),
        v.get("8453", 0), v.get("143", 0)))

print("\n=== training_rows(signal) daily count per network 2026-08-03.. (entry_ts epoch) ===")
cur = con.execute("""
    SELECT date(entry_ts,'unixepoch') d, network_id, feature_version fv, COUNT(*) n
      FROM training_rows WHERE kind='signal' AND entry_ts >= strftime('%s','2026-08-03')
     GROUP BY 1,2,3 ORDER BY 1,2,3""")
rowsd = {}
for d, net, fv, n in cur:
    rowsd.setdefault(d, {})[(str(net), fv)] = n
print("%-12s %s" % ("day", "net:fv=count"))
for d in sorted(rowsd):
    print("%-12s %s" % (d, "  ".join(f"{k[0]}:fv{k[1]}={v}" for k, v in sorted(rowsd[d].items()))))
con.close()
