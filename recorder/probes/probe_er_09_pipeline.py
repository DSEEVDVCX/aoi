"""Where do 4663/8453 rows disappear? outcomes / watch_windows / bars chain. Read-only."""
import os, sqlite3
import config
from probe_er_fam import POP

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== outcomes schema ===")
cols = [c["name"] for c in con.execute("PRAGMA table_info(outcomes)")]
print("  ", cols)
print()
print("=== outcomes: kind/status by network (join via signal? check columns) ===")
for r in con.execute("SELECT * FROM outcomes LIMIT 2"):
    d = dict(r)
    for k in list(d):
        if isinstance(d[k], (bytes, bytearray)):
            d[k] = f"<blob {len(d[k])}>"
    print("  ", d)
print()

if "network_id" in cols:
    print("=== outcomes per day per network (from 2026-08-05) ===")
    tcol = "entry_ts" if "entry_ts" in cols else ("ts" if "ts" in cols else None)
    print("  time col:", tcol)
    q = f"""SELECT date({tcol},'unixepoch') d, network_id, COUNT(*) n,
              SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok
            FROM outcomes WHERE kind='signal' AND {tcol} >= strftime('%s','2026-08-05')
           GROUP BY 1,2 ORDER BY 1,2"""
    cur = {}; nets = set()
    for r in con.execute(q):
        cur.setdefault(r["d"], {})[r["network_id"]] = (r["n"], r["ok"])
        nets.add(r["network_id"])
    nets = sorted(nets)
    print("  day        " + "".join(f"{n[:10]:>15s}" for n in nets))
    for d in sorted(cur):
        print(f"  {d} " + "".join(f"{str(cur[d].get(n,(0,0))):>15s}" for n in nets))

print()
print("=== watch_windows per day per network (first_seen_at), from 2026-08-05 ===")
q = """SELECT substr(first_seen_at,1,10) d, network_id, COUNT(*) n,
         SUM(CASE WHEN is_control=1 THEN 1 ELSE 0 END) ctrl
       FROM watch_windows WHERE first_seen_at >= '2026-08-05' GROUP BY 1,2 ORDER BY 1,2"""
cur = {}; nets = set()
for r in con.execute(q):
    cur.setdefault(r["d"], {})[r["network_id"]] = r["n"]
    nets.add(r["network_id"])
nets = sorted(nets)
print("  day        " + "".join(f"{str(n)[:10]:>13s}" for n in nets))
for d in sorted(cur):
    print(f"  {d} " + "".join(f"{cur[d].get(n,0):>13d}" for n in nets))

print()
print("=== watch_windows columns ===")
print("  ", [c["name"] for c in con.execute("PRAGMA table_info(watch_windows)")])
print()
print("=== training_rows(kind=signal) status by network, entry_ts >= 2026-08-11 ===")
for r in con.execute("""SELECT network_id, status, COUNT(*) n FROM training_rows
   WHERE kind='signal' AND entry_ts >= strftime('%s','2026-08-11') GROUP BY 1,2 ORDER BY 1,2"""):
    print("  ", dict(r))
print()
print("=== token_bars: latest ts per network (via watchlist) ===")
for net in ("1399811149", "56", "4663", "8453"):
    r = con.execute("""SELECT COUNT(*) n, MAX(ts) m FROM token_bars
        WHERE token_address IN (SELECT token_address FROM watchlist WHERE network_id=?)""", (net,)).fetchone()
    print(f"  net={net:12s} bars={r['n']:8d} max_ts={r['m']}")
print()
print("=== watchlist by network: active, watch_until ===")
for r in con.execute("""SELECT network_id, COUNT(*) n, SUM(active) act,
      MAX(watch_until) mx, MIN(watch_until) mn FROM watchlist GROUP BY 1 ORDER BY 2 DESC"""):
    print("  ", dict(r))
