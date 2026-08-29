"""Family emptiness x network, x day(entry_ts), x day(built_at). Read-only."""
import os, sqlite3
import config
from probe_er_fam import FAMILIES, WITNESS, POP, empty_expr, n_empty_expr

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

fams = list(FAMILIES)

# ---------- per-network family emptiness matrix ----------
sel = ", ".join(f"SUM(CASE WHEN {empty_expr(f)} THEN 1 ELSE 0 END) AS e_{f}" for f in fams)
wsel = ", ".join(f"SUM(CASE WHEN {WITNESS[f]} IS NULL THEN 1 ELSE 0 END) AS w_{f}" for f in fams)
q = f"""SELECT network_id, COUNT(*) n, {sel}, {wsel}
          FROM training_rows WHERE {POP} GROUP BY network_id ORDER BY n DESC"""
rows = con.execute(q).fetchall()

print("=== ENTIRELY-NULL family counts by network_id (pct of that network's rows) ===")
hdr = f"{'family':24s}" + "".join(f"{r['network_id'][:10]:>16s}" for r in rows) + f"{'ALL':>16s}"
print(hdr)
tot = {f: 0 for f in fams}
N = sum(r["n"] for r in rows)
print(f"{'(rows)':24s}" + "".join(f"{r['n']:>16d}" for r in rows) + f"{N:>16d}")
for f in fams:
    line = f"{f:24s}"
    for r in rows:
        v = r[f"e_{f}"]
        tot[f] += v
        line += f"{v:>9d}/{100.0*v/r['n']:5.1f}%"
    line += f"{tot[f]:>9d}/{100.0*tot[f]/N:5.1f}%"
    print(line)

print()
print("=== WITNESS-column NULL counts by network (collector never produced a row <= t0) ===")
print(f"{'family(witness)':38s}" + "".join(f"{r['network_id'][:10]:>16s}" for r in rows))
for f in fams:
    line = f"{f+'('+WITNESS[f]+')':38s}"
    for r in rows:
        v = r[f"w_{f}"]
        line += f"{v:>9d}/{100.0*v/r['n']:5.1f}%"
    print(line)

# ---------- distribution of #empty families per network ----------
print()
print("=== #empty families distribution, per network ===")
q2 = f"""SELECT network_id, ({n_empty_expr()}) k, COUNT(*) n
           FROM training_rows WHERE {POP} GROUP BY 1,2 ORDER BY 1,2"""
cur = {}
for r in con.execute(q2):
    cur.setdefault(r["network_id"], {})[r["k"]] = r["n"]
for net, d in sorted(cur.items(), key=lambda kv: -sum(kv[1].values())):
    print(f"  net={net:12s} " + "  ".join(f"{k}:{v}" for k, v in sorted(d.items())))

# ---------- by day of entry_ts ----------
print()
print("=== by DAY of entry_ts: rows, avg #empty families, rows with >=6 empty ===")
q3 = f"""SELECT date(entry_ts,'unixepoch') d, COUNT(*) n,
                AVG({n_empty_expr()}) avg_empty,
                SUM(CASE WHEN ({n_empty_expr()}) >= 6 THEN 1 ELSE 0 END) ge6,
                SUM(CASE WHEN {empty_expr('market_snapshot')} THEN 1 ELSE 0 END) e_mkt,
                SUM(CASE WHEN {empty_expr('token_static')} THEN 1 ELSE 0 END) e_static,
                SUM(CASE WHEN {empty_expr('flow')} THEN 1 ELSE 0 END) e_flow,
                SUM(CASE WHEN {empty_expr('chain_ownership')} THEN 1 ELSE 0 END) e_chain,
                SUM(CASE WHEN {empty_expr('top_trader_periods')} THEN 1 ELSE 0 END) e_tt
           FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 1"""
print(f"  {'day':12s}{'n':>6s}{'avg':>7s}{'>=6':>7s}{'mkt':>7s}{'static':>8s}{'flow':>7s}{'chain':>7s}{'toptr':>7s}")
for r in con.execute(q3):
    print(f"  {r['d']:12s}{r['n']:>6d}{r['avg_empty']:>7.2f}{r['ge6']:>7d}"
          f"{r['e_mkt']:>7d}{r['e_static']:>8d}{r['e_flow']:>7d}{r['e_chain']:>7d}{r['e_tt']:>7d}")

# ---------- by day of built_at ----------
print()
print("=== by DAY of built_at ===")
q4 = f"""SELECT substr(built_at,1,10) d, COUNT(*) n, AVG({n_empty_expr()}) avg_empty,
                SUM(CASE WHEN ({n_empty_expr()}) >= 6 THEN 1 ELSE 0 END) ge6,
                SUM(CASE WHEN {empty_expr('market_snapshot')} THEN 1 ELSE 0 END) e_mkt,
                SUM(CASE WHEN {empty_expr('token_static')} THEN 1 ELSE 0 END) e_static
           FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 1"""
for r in con.execute(q4):
    print(f"  {r['d']:12s}{r['n']:>6d} avg={r['avg_empty']:.2f} ge6={r['ge6']:5d} e_mkt={r['e_mkt']:5d} e_static={r['e_static']:5d}")
