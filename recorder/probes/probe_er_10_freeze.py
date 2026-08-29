"""Quantify the EVM training-row freeze backlog. Read-only."""
import os, sqlite3
import config
from probe_er_fam import POP

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("config.EVM_NETWORKS       =", config.EVM_NETWORKS)
print("config.EVM_REPLAY_NETWORKS=", config.EVM_REPLAY_NETWORKS)
excluded = sorted({*(str(n) for n in config.EVM_NETWORKS), *(str(n) for n in config.EVM_REPLAY_NETWORKS)})
print("excluded set              =", excluded)
for k in ("evm_ledger_rebuild_required", "evm_training_rebuild_started",
          "evm_ledger_generation", "evm_admission_paused", "build_rows_last_run_at",
          "build_rows_last_stats"):
    r = con.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    print(f"  meta[{k}] = {r['value'] if r else None}")
print()

ph = ", ".join("?" for _ in excluded)
q = f"""SELECT o.network_id, COUNT(*) n,
   SUM(CASE WHEN o.status='ok' THEN 1 ELSE 0 END) ok,
   SUM(CASE WHEN o.is_independent=1 AND o.status='ok' THEN 1 ELSE 0 END) ok_indep,
   MIN(date(o.entry_ts,'unixepoch')) d0, MAX(date(o.entry_ts,'unixepoch')) d1
  FROM outcomes o
 WHERE o.kind='signal' AND o.status IN ('ok','no_bars')
   AND COALESCE(o.network_id,'') IN ({ph})
   AND NOT EXISTS (SELECT 1 FROM training_rows r
        WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
 GROUP BY 1 ORDER BY 2 DESC"""
print("=== labeled outcomes on the excluded networks with NO training_row at fv=12 ===")
tot = 0
for r in con.execute(q, excluded):
    tot += r["n"]
    print(f"  net={r['network_id']:12s} outcomes={r['n']:6d} ok={r['ok']:6d} ok&indep={r['ok_indep']:6d}  {r['d0']} .. {r['d1']}")
print("  TOTAL unbuilt:", tot)
print()

print("=== same, but restricted to entry_ts >= 2026-08-11 (the freeze window) ===")
q2 = q.replace("GROUP BY 1", "AND o.entry_ts >= strftime('%s','2026-08-11') GROUP BY 1")
tot = 0; toti = 0
for r in con.execute(q2, excluded):
    tot += r["n"]; toti += r["ok_indep"]
    print(f"  net={r['network_id']:12s} outcomes={r['n']:6d} ok={r['ok']:6d} ok&indep={r['ok_indep']:6d}  {r['d0']} .. {r['d1']}")
print("  TOTAL unbuilt since 2026-08-11:", tot, " of which ok&independent:", toti)
print()

print("=== control: same query for the NON-excluded networks (should be ~0 / just the labeler lag) ===")
q3 = f"""SELECT o.network_id, COUNT(*) n, MIN(date(o.entry_ts,'unixepoch')) d0,
        MAX(date(o.entry_ts,'unixepoch')) d1
  FROM outcomes o
 WHERE o.kind='signal' AND o.status IN ('ok','no_bars')
   AND COALESCE(o.network_id,'') NOT IN ({ph})
   AND NOT EXISTS (SELECT 1 FROM training_rows r
        WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
 GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q3, excluded):
    print(f"  net={r['network_id']:12s} unbuilt={r['n']:6d}  {r['d0']} .. {r['d1']}")
print()

print("=== population: max entry_ts per network (days behind today 2026-08-22) ===")
for r in con.execute(f"""SELECT network_id, COUNT(*) n, date(MAX(entry_ts),'unixepoch') last_day,
      (strftime('%s','2026-08-22') - MAX(entry_ts))/86400.0 days_behind
   FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net={r['network_id']:12s} n={r['n']:6d} last_entry_day={r['last_day']} days_behind={r['days_behind']:.1f}")
print()

print("=== 4663/8453 population rows: how many empty families each? ===")
from probe_er_fam import n_empty_expr
for net in ("4663", "8453", "1399811149", "56"):
    q4 = f"""SELECT ({n_empty_expr()}) k, COUNT(*) n FROM training_rows
             WHERE {POP} AND network_id=? GROUP BY 1 ORDER BY 1"""
    d = {r["k"]: r["n"] for r in con.execute(q4, (net,))}
    tot = sum(d.values())
    avg = sum(k * v for k, v in d.items()) / tot if tot else 0
    print(f"  net={net:12s} n={tot:6d} avg_empty_families={avg:.2f}  dist={d}")
