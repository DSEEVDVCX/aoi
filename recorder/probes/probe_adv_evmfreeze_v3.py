"""Adversarial probe 3: is the interlock converging, and what exactly is stale?
Read-only.
"""
import json
import os
import sqlite3
import sys
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import features  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()  # noqa: E731

print("=== J. fv8 vs fv12 entry_ts ranges on 4663 (who covers what) ===")
for r in q("""SELECT feature_version fv, kind, COUNT(*) n,
                     MIN(entry_ts) lo, MAX(entry_ts) hi,
                     datetime(MIN(entry_ts),'unixepoch') lo_iso,
                     datetime(MAX(entry_ts),'unixepoch') hi_iso
              FROM training_rows WHERE network_id='4663'
              GROUP BY fv, kind ORDER BY fv, kind"""):
    print(f"  fv={r['fv']:<3} kind={r['kind']:<7} n={r['n']:<6} "
          f"entry {r['lo_iso']} .. {r['hi_iso']}")

print("\n=== K. outcomes with NO training_rows row AT ALL (any fv) ===")
for r in q("""SELECT COALESCE(o.network_id,'<NULL>') net, o.kind, COUNT(*) n,
                     datetime(MIN(o.entry_ts),'unixepoch') lo,
                     datetime(MAX(o.entry_ts),'unixepoch') hi
              FROM outcomes o
              WHERE o.status='ok'
                AND NOT EXISTS (SELECT 1 FROM training_rows r
                                 WHERE r.kind=o.kind AND r.key=o.key)
              GROUP BY net, o.kind ORDER BY n DESC"""):
    print(f"  net={r['net']:<12} kind={r['kind']:<7} n={r['n']:<7} "
          f"entry {r['lo']} .. {r['hi']}")

print("\n=== L. replay convergence vs the captured cohort ===")
cohort = json.loads(q("SELECT CAST(value AS TEXT) v FROM meta "
                      "WHERE key='evm_repair_cohort'")[0]["v"])
windows = cohort["replay_windows"]
tok = {(w["network_id"], w["token_address"].lower()) for w in windows}
print(f"  cohort replay windows={len(windows)} distinct (net,token)={len(tok)}")
st = {(r["network_id"], str(r["token_address"]).lower()): r
      for r in q("SELECT network_id, token_address, status, last_try_at, "
                 "snapshots, transfers FROM evm_replay_state")}
FINAL = {"done", "empty", "error", "negative"}
cnt = collections.Counter()
for k in tok:
    row = st.get(k)
    cnt[(row["status"] if row else "<no state>") or "<NULL>"] += 1
print("  cohort tokens by replay status:", dict(cnt.most_common()))
final_n = sum(v for k, v in cnt.items() if k in FINAL)
print(f"  cohort tokens in a FINAL replay status = {final_n} / {len(tok)} "
      f"({100.0*final_n/max(1,len(tok)):.1f}%)")

print("\n=== M. is the repair moving? last_try_at recency ===")
for t in ("evm_replay_state", "evm_backfill_state"):
    for r in q(f"""SELECT substr(last_try_at,1,10) d, COUNT(*) n FROM {t}
                   GROUP BY d ORDER BY d DESC LIMIT 8"""):
        print(f"  {t} last_try_at {r['d']}: {r['n']}")
for r in q("""SELECT COUNT(*) n FROM evm_replay_state
              WHERE status='done' AND last_try_at >= '2026-08-21'"""):
    print(f"  replay rows reaching done since 2026-08-21: {r['n']}")

print("\n=== N. cohort backfill convergence ===")
ab = cohort["active_backfills"]
print(f"  cohort active_backfills = {len(ab)}")
bs = {(r["network_id"], str(r["token_address"]).lower()): r["status"]
      for r in q("SELECT network_id, token_address, status FROM evm_backfill_state")}
c2 = collections.Counter()
for item in ab:
    c2[bs.get((str(item["network_id"]), str(item["token_address"]).lower()))
       or "<no state>"] += 1
print("  cohort backfill targets by status:", dict(c2.most_common()))

print("\n=== O. concentration columns on EVM training rows (post-reset NULLing) ===")
cols = ("onchain_top1_pct", "onchain_top10_pct", "onchain_holder_count",
        "onchain_age_min")
sel = ", ".join(f"SUM({c} IS NOT NULL) nn_{c}" for c in cols)
for r in q(f"""SELECT COALESCE(network_id,'<NULL>') net, feature_version fv,
                      COUNT(*) n, {sel}
               FROM training_rows WHERE kind='signal'
               GROUP BY net, fv ORDER BY net, fv"""):
    print(f"  net={r['net']:<12} fv={r['fv']:<3} n={r['n']:<6} " +
          " ".join(f"{c}={r['nn_'+c]}" for c in cols))

print("\n=== P. chain_concentration rows available for EVM now ===")
for r in q("""SELECT network_id, COALESCE(is_replay,0) rep, COUNT(*) n,
                     MIN(recorded_at) lo, MAX(recorded_at) hi
              FROM chain_concentration WHERE network_id IN ('4663','8453','143')
              GROUP BY network_id, rep ORDER BY network_id, rep"""):
    print(f"  net={r['network_id']:<6} is_replay={r['rep']} n={r['n']:<7} "
          f"{str(r['lo'])[:19]} .. {str(r['hi'])[:19]}")
con.close()
