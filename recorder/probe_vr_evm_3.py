import os, sqlite3, json, time, config
from datetime import UTC, datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, p=()): return con.execute(sql, p).fetchall()

cohort = json.loads(q("SELECT value FROM meta WHERE key='evm_repair_cohort'")[0]["value"])
print("cohort captured_at =", cohort["captured_at"], " networks =", cohort["networks"])
print("cohort active_backfills =", len(cohort["active_backfills"]),
      " replay_windows =", len(cohort["replay_windows"]))

live = {str(n) for n in config.EVM_NETWORKS}
pending = 0
by_net = {}
for it in cohort["active_backfills"]:
    net = str(it["network_id"])
    if net not in live: continue
    r = q("SELECT status FROM evm_backfill_state WHERE network_id=? AND token_address=?",
          (net, it["token_address"]))
    st = (r[0]["status"] if r else None)
    if st != "done":
        pending += 1
        by_net[net] = by_net.get(net, 0) + 1
print("\nACTIVE_PENDING (cohort backfills not 'done') =", pending, by_net)

FINAL = ("done","negative","empty","no_time","skip","budget")
keys = {(str(w["network_id"]), str(w["token_address"]).lower()) for w in cohort["replay_windows"]}
now = int(datetime.now(UTC).timestamp())
def ep(s): return int(datetime.fromisoformat(s.replace("Z","+00:00")).timestamp())
mature_by_key = {}
for w in cohort["replay_windows"]:
    k = (str(w["network_id"]), str(w["token_address"]).lower())
    if ep(w["watch_until"]) <= now:
        mature_by_key[k] = max(mature_by_key.get(k, 0), ep(w["watch_until"]))
notfinal = 0
for k in sorted(mature_by_key):
    r = q("SELECT status FROM evm_replay_state WHERE network_id=? AND token_address=?", k)
    st = (r[0]["status"] if r else None)
    if st not in FINAL:
        notfinal += 1
print("REPLAY keys total =", len(keys), " mature =", len(mature_by_key),
      " NOT-final (approx replay_pending) =", notfinal)

print("\nevm_backfill_state status distribution on excluded nets:")
for r in q("""SELECT network_id, COALESCE(status,'(null)') s, COUNT(*) n
              FROM evm_backfill_state WHERE network_id IN ('143','4663','8453')
              GROUP BY 1,2 ORDER BY 1,3 DESC"""):
    print(f"  net={r['network_id']:<6} status={r['s']:<10} n={r['n']}")

print("\nevm_replay_state status distribution:")
for r in q("""SELECT network_id, COALESCE(status,'(null)') s, COUNT(*) n
              FROM evm_replay_state GROUP BY 1,2 ORDER BY 1,3 DESC"""):
    print(f"  net={r['network_id']:<6} status={r['s']:<10} n={r['n']}")

print("\nchain_concentration rows on excluded nets (repaired data accumulating?):")
for r in q("""SELECT network_id, COALESCE(is_replay,0) rp, COUNT(*) n,
                     MIN(ts) mn, MAX(ts) mx
              FROM chain_concentration WHERE network_id IN ('143','4663','8453')
              GROUP BY 1,2 ORDER BY 1,2"""):
    print(f"  net={r['network_id']:<6} is_replay={r['rp']} n={r['n']:<7} ts {r['mn']} .. {r['mx']}")
con.close()
