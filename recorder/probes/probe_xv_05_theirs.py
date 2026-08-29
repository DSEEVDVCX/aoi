"""Adversarial verification, step 5: run the CLAIM's exact SQL verbatim. READ-ONLY."""
import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = """SELECT o.network_id, COUNT(*) n, date(MIN(o.entry_ts),'unixepoch') first_d,
       date(MAX(o.entry_ts),'unixepoch') last_d FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
   AND o.entry_ts >= 1785018927
   AND COALESCE(o.network_id,'') IN ('4663','8453','143')
   AND NOT EXISTS (SELECT 1 FROM training_rows r
        WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
 GROUP BY 1"""
t = 0
for r in con.execute(q):
    t += r["n"]
    print(f"  net={r['network_id']:<12} n={r['n']:>6,}  {r['first_d']} .. {r['last_d']}")
print(f"  their-SQL TOTAL = {t:,}")

print("\n-- fv8 stranded, their breakdown --")
for r in con.execute("""
SELECT COALESCE(network_id,'') net, COUNT(*) n,
       SUM(CASE WHEN is_independent=1 THEN 1 ELSE 0 END) indep
  FROM training_rows WHERE feature_version=8 AND is_live=1 GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net={r['net']:<12} n={r['n']:>7,}  is_independent=1: {r['indep']:>6,}")

print("\n-- signal_events still recorded on those nets today --")
for r in con.execute("""
SELECT COALESCE(network_id,'') net, COUNT(*) n FROM signal_events
 WHERE ts >= strftime('%s', date('now')) AND COALESCE(network_id,'') IN ('4663','8453','143')
 GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net={r['net']:<12} today n={r['n']:>6,}")
con.close()
