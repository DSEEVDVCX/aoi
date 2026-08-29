import os, sqlite3, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = features.FEATURE_VERSION

def iso(e):
    if e is None:
        return "-"
    return dt.datetime.fromtimestamp(int(e), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

EVM = sorted({*(str(n) for n in config.EVM_NETWORKS), *(str(n) for n in config.EVM_REPLAY_NETWORKS)})
print("builder-excluded networks (union EVM_NETWORKS+EVM_REPLAY_NETWORKS) =", EVM)

# ---- Q1: MY OWN query. LEFT JOIN, split by kind, and by whether a row exists at ANY fv.
print("\n=== Q1 outcomes(ok/no_bars) by network+kind: has fv=12 row / has only older-fv row / no row at all ===")
q1 = """
SELECT o.network_id AS net, o.kind AS kind,
       COUNT(*) AS n_out,
       SUM(CASE WHEN r.key IS NULL THEN 1 ELSE 0 END) AS no_row_at_all,
       SUM(CASE WHEN r.key IS NOT NULL AND r.feature_version <> ? THEN 1 ELSE 0 END) AS row_stale_fv,
       SUM(CASE WHEN r.feature_version = ? THEN 1 ELSE 0 END) AS row_cur_fv,
       MAX(CASE WHEN r.feature_version = ? THEN o.entry_ts END) AS max_ts_cur,
       MAX(o.entry_ts) AS max_ts_out
  FROM outcomes o
  LEFT JOIN training_rows r ON r.kind=o.kind AND r.key=o.key
 WHERE o.status IN ('ok','no_bars')
 GROUP BY 1,2 ORDER BY n_out DESC
"""
tot_missing = 0
tot_missing_evm = 0
rows = con.execute(q1, (FV, FV, FV)).fetchall()
print(f"{'net':>12} {'kind':<7} {'n_out':>7} {'no_row':>7} {'staleFV':>7} {'curFV':>7}  {'max curFV ts':<22} {'max out ts'}")
for r in rows:
    miss = r["no_row_at_all"] + r["row_stale_fv"]
    tot_missing += miss
    if str(r["net"]) in EVM:
        tot_missing_evm += miss
    print(f"{str(r['net']):>12} {r['kind']:<7} {r['n_out']:>7} {r['no_row_at_all']:>7} {r['row_stale_fv']:>7} {r['row_cur_fv']:>7}  {iso(r['max_ts_cur']):<22} {iso(r['max_ts_out'])}")
print(f"TOTAL outcomes lacking a fv={FV} row (all kinds/nets): {tot_missing}   of which excluded-EVM nets: {tot_missing_evm}")

# ---- Q2: their exact query, for agreement check
print("\n=== Q2 THEIR exact query ===")
for r in con.execute("""SELECT o.network_id, COUNT(*) n, MIN(o.entry_ts) mn, MAX(o.entry_ts) mx
  FROM outcomes o WHERE o.status IN ('ok','no_bars')
   AND NOT EXISTS (SELECT 1 FROM training_rows r WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
 GROUP BY 1 ORDER BY n DESC"""):
    print(f"  net={str(r[0]):>12} n={r[1]:>7}  {iso(r[2])} .. {iso(r[3])}")

# ---- Q3: training_rows by network x feature_version x is_live
print("\n=== Q3 training_rows: network x feature_version x is_live ===")
for r in con.execute("""SELECT network_id, feature_version, is_live, COUNT(*) n,
                               MIN(entry_ts) mn, MAX(entry_ts) mx
                          FROM training_rows GROUP BY 1,2,3 ORDER BY 1,2,3"""):
    print(f"  net={str(r['network_id']):>12} fv={r['feature_version']:>3} live={r['is_live']} n={r['n']:>7}  {iso(r['mn'])} .. {iso(r['mx'])}")

# ---- Q4: model population (base table, cheap filters) per network
print("\n=== Q4 model population (base table filters) per network ===")
cur_meta = con.execute("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)").fetchone()[0]
print("  current_feature_version(meta) =", cur_meta, " features.FEATURE_VERSION =", FV)
q4 = """SELECT network_id, COUNT(*) n, MIN(entry_ts) mn, MAX(entry_ts) mx
          FROM training_rows
         WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
           AND is_independent=1 AND feature_version = ?
         GROUP BY 1 ORDER BY n DESC"""
tot = 0
for r in con.execute(q4, (cur_meta,)):
    tot += r["n"]
    print(f"  net={str(r['network_id']):>12} n={r['n']:>6}  {iso(r['mn'])} .. {iso(r['mx'])}")
print("  model population total =", tot)
con.close()
