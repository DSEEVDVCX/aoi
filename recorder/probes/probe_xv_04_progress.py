"""Adversarial verification, step 4: is the EVM repair progressing or stalled? READ-ONLY."""
import os, sqlite3, json, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("== evm_replay_state: status x day of last_try_at ==")
for r in con.execute("""
SELECT COALESCE(status,'(null)') st, substr(last_try_at,1,10) d, COUNT(*) n
  FROM evm_replay_state WHERE network_id IN ('4663','8453','143')
 GROUP BY 1,2 ORDER BY 2,1"""):
    print(f"  {r['d']}  {r['st']:<10} {r['n']:>5}")

print("\n== evm_backfill_state: status x day of last_try_at (cohort networks) ==")
for r in con.execute("""
SELECT network_id net, COALESCE(status,'(null)') st, substr(last_try_at,1,10) d, COUNT(*) n
  FROM evm_backfill_state WHERE network_id IN ('4663','8453','143')
 GROUP BY 1,2,3 ORDER BY 3,1,2"""):
    print(f"  {r['d']}  net={r['net']:<6} {r['st']:<10} {r['n']:>5}")

print("\n== chain_concentration: replay vs live rows written per day (EVM) ==")
for r in con.execute("""
SELECT substr(created_at,1,10) d, COALESCE(is_replay,0) rep, COUNT(*) n
  FROM chain_concentration WHERE network_id IN ('4663','8453','143')
   AND created_at >= '2026-08-14' GROUP BY 1,2 ORDER BY 1,2"""):
    print(f"  {r['d']}  is_replay={r['rep']}  {r['n']:>7,}")
con.close()
