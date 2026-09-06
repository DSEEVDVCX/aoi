"""Final numbers: overall empty-family distribution, stale-fv shadow rows. Read-only."""
import os, sqlite3
import config
from probe_er_fam import FAMILIES, POP, empty_expr, n_empty_expr

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== overall distribution of #entirely-NULL families ===")
q = f"SELECT ({n_empty_expr()}) k, COUNT(*) n FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 1"
rows = con.execute(q).fetchall()
N = sum(r["n"] for r in rows)
print("  N =", N)
for r in rows:
    print(f"   {r['k']} empty families: {r['n']:6d}  {100.0*r['n']/N:5.2f}%")
print()

print("=== barren cohort (8 empty) by network ===")
core8 = ["top_trader_periods", "token_static", "market_snapshot", "chain_ownership",
         "onchain_concentration", "onchain_sol_auth", "onchain_evm_contract", "flow"]
cond = " AND ".join(empty_expr(f) for f in core8)
for r in con.execute(f"""SELECT network_id, COUNT(*) n, MIN(date(entry_ts,'unixepoch')) d0,
      MAX(date(entry_ts,'unixepoch')) d1 FROM training_rows WHERE {POP} AND {cond}
      GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"   net={r['network_id']:12s} {r['n']:5d}  {r['d0']} .. {r['d1']}")
print()

print("=== stale-feature-version shadow rows (fv=8 with no fv=12 twin) ===")
for r in con.execute("""SELECT kind, network_id, COUNT(*) n FROM training_rows a
   WHERE a.feature_version=8 AND NOT EXISTS (SELECT 1 FROM training_rows b
     WHERE b.kind=a.kind AND b.key=a.key AND b.feature_version=12)
   GROUP BY 1,2 ORDER BY 3 DESC"""):
    print("  ", dict(r))
r = con.execute("""SELECT COUNT(*) n FROM training_rows a WHERE a.feature_version=8
   AND NOT EXISTS (SELECT 1 FROM training_rows b WHERE b.kind=a.kind AND b.key=a.key
   AND b.feature_version=12)""").fetchone()
print("   TOTAL shadow rows:", r["n"])
r = con.execute("SELECT COUNT(*) n FROM training_rows").fetchone()
print("   total training_rows:", r["n"])
print()

print("=== per-family EMPTY counts, whole population, final numbers ===")
sel = ", ".join(f"SUM(CASE WHEN {empty_expr(f)} THEN 1 ELSE 0 END) e_{f}" for f in FAMILIES)
r = con.execute(f"SELECT COUNT(*) N, {sel} FROM training_rows WHERE {POP}").fetchone()
for f in FAMILIES:
    print(f"   {f:24s} {r['e_'+f]:6d} / {r['N']}  {100.0*r['e_'+f]/r['N']:5.2f}%")
print()

print("=== multi_user_buy-only columns: filled count ===")
r = con.execute(f"""SELECT COUNT(*) N,
   SUM(CASE WHEN minutes IS NOT NULL THEN 1 ELSE 0 END) f_minutes,
   SUM(CASE WHEN unique_traders IS NOT NULL THEN 1 ELSE 0 END) f_uniq,
   SUM(CASE WHEN are_top_traders IS NOT NULL THEN 1 ELSE 0 END) f_att,
   SUM(CASE WHEN top_traders_listed = 0 THEN 1 ELSE 0 END) z_listed
  FROM training_rows WHERE {POP}""").fetchone()
print("  ", dict(r))
