"""Adversarial probe 2: when did the EVM freeze actually start, and is the
interlock progressing? Read-only.
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import features  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()  # noqa: E731

print("=== A. every meta key touching the repair (values truncated) ===")
for r in q("""SELECT key, substr(CAST(value AS TEXT),1,220) v,
                     length(CAST(value AS TEXT)) len FROM meta
              WHERE key LIKE '%repair%' OR key LIKE '%rebuild%'
                 OR key LIKE '%ledger%' OR key LIKE '%replay%'
                 OR key LIKE '%admission%' OR key='current_feature_version'
              ORDER BY key"""):
    print(f"  {r['key']} (len={r['len']}) = {r['v']}")

print("\n=== B. repair cohort captured_at (the true freeze start) ===")
raw = q("SELECT CAST(value AS TEXT) v FROM meta WHERE key='evm_repair_cohort'")
if not raw:
    print("  evm_repair_cohort: ABSENT -> finalize_training has no cutoff")
else:
    c = json.loads(raw[0]["v"])
    print("  captured_at =", c.get("captured_at"))
    print("  networks    =", c.get("networks"))
    print("  active_backfills =", len(c.get("active_backfills", [])))
    print("  replay_windows   =", len(c.get("replay_windows", [])))

print("\n=== C. fv12 rows on the EXCLUDED nets: do they exist, and when did"
      " building stop? ===")
for r in q("""SELECT COALESCE(network_id,'<NULL>') net, kind, COUNT(*) n,
                     MIN(built_at) oldest, MAX(built_at) newest,
                     MIN(entry_ts) min_entry, MAX(entry_ts) max_entry
              FROM training_rows WHERE feature_version=12
              GROUP BY net, kind ORDER BY n DESC"""):
    print(f"  net={r['net']:<12} kind={r['kind']:<7} n={r['n']:<7} "
          f"oldest={r['oldest'][:19]} newest={r['newest'][:19]} "
          f"entry_ts {r['min_entry']}..{r['max_entry']}")

print("\n=== D. daily built_at histogram, EVM nets only, fv12 ===")
for r in q("""SELECT substr(built_at,1,10) d, COUNT(*) n
              FROM training_rows
              WHERE feature_version=12 AND network_id IN ('4663','8453','143')
              GROUP BY d ORDER BY d"""):
    print(f"  {r['d']}  {r['n']}")

print("\n=== E. daily built_at histogram, NON-EVM nets, fv12 (control) ===")
for r in q("""SELECT substr(built_at,1,10) d, COUNT(*) n
              FROM training_rows
              WHERE feature_version=12
                AND COALESCE(network_id,'') NOT IN ('4663','8453','143')
              GROUP BY d ORDER BY d"""):
    print(f"  {r['d']}  {r['n']}")

print("\n=== F. interlock progress: replay + backfill completion ===")
for r in q("""SELECT network_id, COALESCE(status,'<NULL>') st, COUNT(*) n
              FROM evm_replay_state GROUP BY network_id, st ORDER BY network_id, n DESC"""):
    print(f"  replay net={r['network_id']:<8} status={r['st']:<12} {r['n']}")
for r in q("""SELECT network_id, COALESCE(status,'<NULL>') st, COUNT(*) n
              FROM evm_backfill_state GROUP BY network_id, st ORDER BY network_id, n DESC"""):
    print(f"  backfill net={r['network_id']:<8} status={r['st']:<12} {r['n']}")
r = q("""SELECT COUNT(*) n FROM watchlist w
         LEFT JOIN evm_backfill_state b
           ON b.network_id=w.network_id AND b.token_address=w.token_address
         WHERE w.active=1 AND w.network_id IN ('4663','8453','143')
           AND COALESCE(b.status,'') <> 'done'""")[0]
print(f"  active_pending (inspect's own SQL, no cohort) = {r['n']}")

print("\n=== G. chain_concentration on EVM nets after the wipe ===")
for r in q("""SELECT network_id, COALESCE(is_replay,0) rep, COUNT(*) n,
                     MIN(captured_at) oldest, MAX(captured_at) newest
              FROM chain_concentration WHERE network_id IN ('4663','8453','143')
              GROUP BY network_id, rep ORDER BY network_id, rep"""):
    print(f"  net={r['network_id']:<8} is_replay={r['rep']} n={r['n']:<7} "
          f"{str(r['oldest'])[:19]} .. {str(r['newest'])[:19]}")

print("\n=== H. do the surviving fv12 EVM rows still carry concentration"
      " features, or were they NULLed by reset()? ===")
cols = ("onchain_top1_pct", "onchain_top10_pct", "onchain_holder_count",
        "onchain_age_min")
sel = ", ".join(f"SUM({c} IS NOT NULL) {c}" for c in cols)
for r in q(f"""SELECT COALESCE(network_id,'<NULL>') net, feature_version fv,
                      COUNT(*) n, {sel}
               FROM training_rows
               WHERE network_id IN ('4663','8453','143') AND kind='signal'
               GROUP BY net, fv ORDER BY net, fv"""):
    print(f"  net={r['net']:<8} fv={r['fv']:<4} n={r['n']:<7} " +
          " ".join(f"{c}={r[c]}" for c in cols))

print("\n=== I. same for the model population (non-EVM control) ===")
for r in q(f"""SELECT COALESCE(network_id,'<NULL>') net, COUNT(*) n, {sel}
               FROM training_rows
               WHERE kind='signal' AND feature_version=12
                 AND COALESCE(network_id,'') NOT IN ('4663','8453','143')
               GROUP BY net ORDER BY n DESC"""):
    print(f"  net={r['net']:<12} n={r['n']:<7} " +
          " ".join(f"{c}={r[c]}" for c in cols))
con.close()
