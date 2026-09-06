"""Per-network training_rows recency + outcomes coverage."""
import os, sqlite3, datetime as dt
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row
def d(e):
    return None if e is None else dt.datetime.fromtimestamp(int(e), dt.timezone.utc).isoformat()

print("signal_events.ts typeof:", con.execute("SELECT typeof(ts) t, ts FROM signal_events LIMIT 1").fetchone()[0])

print("\n-- per network: max entry_ts in training_rows(signal) --")
for r in con.execute("""SELECT network_id, COUNT(*) n, MIN(entry_ts) mn, MAX(entry_ts) mx
                          FROM training_rows WHERE kind='signal' GROUP BY 1 ORDER BY n DESC"""):
    print(f"   net={r['network_id']:12s} n={r['n']:7d}  {d(r['mn'])} -> {d(r['mx'])}")

print("\n-- per network: signals total, signals with an outcomes row, training rows --")
print("   outcomes cols:", [x["name"] for x in con.execute("PRAGMA table_info(outcomes)")])
q = """SELECT s.network_id,
              COUNT(*) signals,
              SUM(CASE WHEN EXISTS(SELECT 1 FROM outcomes o WHERE o.kind='signal' AND o.key=s.id) THEN 1 ELSE 0 END) with_outcome,
              SUM(CASE WHEN EXISTS(SELECT 1 FROM training_rows t WHERE t.kind='signal' AND t.key=s.id) THEN 1 ELSE 0 END) with_row
         FROM signal_events s GROUP BY 1 ORDER BY signals DESC"""
for r in con.execute(q):
    print("   ", dict(r))
