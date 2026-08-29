import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(s,p=()): return con.execute(s,p).fetchall()

print("=== chain_concentration on excluded nets (post-reset repair data) ===")
for r in q("""SELECT network_id, COALESCE(is_replay,0) rp, COUNT(*) n,
                     MIN(recorded_at) mn, MAX(recorded_at) mx
              FROM chain_concentration WHERE network_id IN ('143','4663','8453')
              GROUP BY 1,2 ORDER BY 1,2"""):
    print(f"  net={r['network_id']:<6} is_replay={r['rp']} n={r['n']:<7} {str(r['mn'])[:19]} .. {str(r['mx'])[:19]}")

print("\n=== backfill/replay last_try_at freshness on excluded nets (is work happening?) ===")
for r in q("""SELECT 'backfill' src, network_id, COALESCE(status,'(null)') s, COUNT(*) n,
                     MAX(last_try_at) last
              FROM evm_backfill_state WHERE network_id IN ('143','4663','8453')
                AND COALESCE(status,'') <> 'done' GROUP BY 1,2,3
              UNION ALL
              SELECT 'replay', network_id, COALESCE(status,'(null)'), COUNT(*), MAX(last_try_at)
              FROM evm_replay_state WHERE network_id IN ('143','4663','8453')
                AND COALESCE(status,'') NOT IN ('done','negative','empty','no_time','skip','budget')
              GROUP BY 1,2,3 ORDER BY 1,2,4 DESC"""):
    print(f"  {r['src']:<9} net={r['network_id']:<6} status={r['s']:<9} n={r['n']:<4} last_try={str(r['last'])[:19]}")

print("\n=== training_rows total on excluded nets, by fv and kind (what finalize would delete) ===")
for r in q("""SELECT feature_version fv, kind, COUNT(*) n FROM training_rows
              WHERE network_id IN ('143','4663','8453') GROUP BY 1,2 ORDER BY 3 DESC"""):
    print(f"  fv={r['fv']:<3} kind={r['kind']:<9} n={r['n']}")

print("\n=== newest model-population signal per network (does the model lag on EVM?) ===")
for r in q("""SELECT COALESCE(network_id,'(null)') net, COUNT(*) n,
                     MAX(datetime(ts,'unixepoch')) newest_signal
              FROM training_rows
              WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
                AND is_independent=1 AND feature_version=12
              GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net={r['net']:<12} n={r['n']:<6} newest t0={r['newest_signal']}")
con.close()
