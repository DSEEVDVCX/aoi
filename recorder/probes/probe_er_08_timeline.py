"""Network x day timeline: signal_events vs training_rows vs population. Read-only."""
import os, sqlite3
import config
from probe_er_fam import POP

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== signal_events ts type ===")
for r in con.execute("SELECT ts, typeof(ts) t FROM signal_events LIMIT 3"):
    print("   ", dict(r))

print()
print("=== signal_events per day per network, from 2026-08-05 ===")
q = """SELECT date(ts,'unixepoch') d, network_id, COUNT(*) n
        FROM signal_events WHERE ts >= strftime('%s','2026-08-05')
       GROUP BY 1,2 ORDER BY 1,2"""
cur = {}; nets = set()
for r in con.execute(q):
    cur.setdefault(r["d"], {})[r["network_id"]] = r["n"]
    nets.add(r["network_id"])
nets = sorted(nets)
print("  day        " + "".join(f"{n[:10]:>13s}" for n in nets))
for d in sorted(cur):
    print(f"  {d} " + "".join(f"{cur[d].get(n,0):>13d}" for n in nets))

print()
print("=== ALL signal training_rows per entry day per network, from 2026-08-05 ===")
q = """SELECT date(entry_ts,'unixepoch') d, network_id, COUNT(*) n
        FROM training_rows WHERE kind='signal' AND entry_ts >= strftime('%s','2026-08-05')
       GROUP BY 1,2 ORDER BY 1,2"""
cur = {}; nets = set()
for r in con.execute(q):
    cur.setdefault(r["d"], {})[r["network_id"]] = r["n"]
    nets.add(r["network_id"])
nets = sorted(nets)
print("  day        " + "".join(f"{n[:10]:>13s}" for n in nets))
for d in sorted(cur):
    print(f"  {d} " + "".join(f"{cur[d].get(n,0):>13d}" for n in nets))

print()
print("=== POPULATION per entry day per network, from 2026-08-05 ===")
q = f"""SELECT date(entry_ts,'unixepoch') d, network_id, COUNT(*) n
        FROM training_rows WHERE {POP} AND entry_ts >= strftime('%s','2026-08-05')
       GROUP BY 1,2 ORDER BY 1,2"""
cur = {}; nets = set()
for r in con.execute(q):
    cur.setdefault(r["d"], {})[r["network_id"]] = r["n"]
    nets.add(r["network_id"])
nets = sorted(nets)
print("  day        " + "".join(f"{n[:10]:>13s}" for n in nets))
for d in sorted(cur):
    print(f"  {d} " + "".join(f"{cur[d].get(n,0):>13d}" for n in nets))

print()
print("=== 4663 last-seen everywhere ===")
for tbl, q in [
    ("signal_events", "SELECT MAX(ts) m, COUNT(*) n FROM signal_events WHERE network_id='4663'"),
    ("training_rows(signal)", "SELECT MAX(entry_ts) m, COUNT(*) n FROM training_rows WHERE kind='signal' AND network_id='4663'"),
    ("population", f"SELECT MAX(entry_ts) m, COUNT(*) n FROM training_rows WHERE {POP} AND network_id='4663'"),
    ("watch_windows", "SELECT MAX(first_seen_at) m, COUNT(*) n FROM watch_windows WHERE network_id='4663'"),
    ("market_ticks(4663 toks)", "SELECT MAX(recorded_at) m, COUNT(*) n FROM market_ticks WHERE token_address IN (SELECT token_address FROM watchlist WHERE network_id='4663')"),
]:
    r = con.execute(q).fetchone()
    print(f"  {tbl:26s} n={r['n']:8d} max={r['m']}")

print()
print("=== 4663 signal rows: status / is_independent / fv breakdown ===")
for r in con.execute("""SELECT feature_version, status, is_independent, is_live, asset_class, COUNT(*) n
   FROM training_rows WHERE kind='signal' AND network_id='4663' GROUP BY 1,2,3,4,5 ORDER BY n DESC LIMIT 20"""):
    print("  ", dict(r))

print()
print("=== network share: signal_events all-time vs population ===")
for label, q in [
    ("signal_events", "SELECT network_id, COUNT(*) n FROM signal_events GROUP BY 1 ORDER BY 2 DESC"),
    ("population", f"SELECT network_id, COUNT(*) n FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 2 DESC"),
]:
    print(" ", label)
    rows = con.execute(q).fetchall()
    tot = sum(r["n"] for r in rows)
    for r in rows:
        print(f"    net={str(r['network_id']):12s} {r['n']:7d}  {100.0*r['n']/tot:5.1f}%")
