"""Adversarial verification of the 'EVM training-row freeze' claim.

Independent measurement. Read-only. Own queries, not the claimant's.
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402
import features  # noqa: E402
from extract import classify_asset  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()  # noqa: E731

print("features.FEATURE_VERSION =", features.FEATURE_VERSION)
print("config.EVM_NETWORKS =", config.EVM_NETWORKS)
print("config.EVM_REPLAY_NETWORKS =", config.EVM_REPLAY_NETWORKS)
print("config.LIVE_START_TS =", config.LIVE_START_TS)

print("\n=== 1. meta keys (my own list) ===")
for r in q("""SELECT key, CAST(value AS TEXT) v FROM meta
              WHERE key LIKE '%evm%' OR key LIKE '%feature_version%'
                 OR key LIKE 'build_rows%' OR key LIKE '%cohort%'
              ORDER BY key"""):
    print(f"  {r['key']:<46} = {r['v']}")

print("\n=== 2. training_rows by feature_version ===")
for r in q("""SELECT feature_version fv, COUNT(*) n,
                     MIN(built_at) oldest, MAX(built_at) newest
              FROM training_rows GROUP BY fv ORDER BY fv"""):
    print(f"  fv={r['fv']:<4} n={r['n']:<7} oldest={r['oldest']}  newest={r['newest']}")

print("\n=== 3. fv8 rows: network x kind x is_live x asset_class ===")
for r in q("""SELECT COALESCE(network_id,'<NULL>') net, kind, is_live,
                     COALESCE(asset_class,'<NULL>') ac, COUNT(*) n, MAX(built_at) newest
              FROM training_rows WHERE feature_version=8
              GROUP BY net, kind, is_live, ac ORDER BY n DESC"""):
    print(f"  net={r['net']:<12} kind={r['kind']:<7} is_live={r['is_live']} "
          f"ac={r['ac']:<7} n={r['n']:<6} newest={r['newest']}")

print("\n=== 4. builder's exact pending predicate, by network ===")
# reproduce build_training_rows.pending_outcomes WITHOUT the EVM filter,
# then split by whether the EVM filter would drop the row.
evm = sorted({*(str(n) for n in config.EVM_NETWORKS),
              *(str(n) for n in config.EVM_REPLAY_NETWORKS)})
print("  exclusion set the code would build =", tuple(evm))
rows = q(f"""SELECT COALESCE(o.network_id,'') net, o.kind, o.status, COUNT(*) n
             FROM outcomes o
             WHERE o.status IN ('ok','no_bars')
               AND NOT EXISTS (SELECT 1 FROM training_rows r
                    WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=?)
             GROUP BY net, o.kind, o.status ORDER BY n DESC""",
         (features.FEATURE_VERSION,))
tot_in = tot_out = 0
for r in rows:
    tag = "BLOCKED" if r["net"] in evm else "buildable"
    if r["net"] in evm:
        tot_in += r["n"]
    else:
        tot_out += r["n"]
    print(f"  net={r['net'] or '<empty>':<12} kind={r['kind']:<7} "
          f"status={r['status']:<8} n={r['n']:<7} {tag}")
print(f"  TOTAL pending on excluded nets = {tot_in}; on other nets = {tot_out}")

print("\n=== 5. model-population size now (base table, cheap filters) ===")
FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
r = q(f"""SELECT COUNT(*) n, COUNT(DISTINCT token_address) coins FROM training_rows
          WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
            AND is_independent=1 AND feature_version={FV}""")[0]
print(f"  model population = {r['n']} rows / {r['coins']} coins")
for x in q(f"""SELECT COALESCE(network_id,'<NULL>') net, COUNT(*) n FROM training_rows
               WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
                 AND is_independent=1 AND feature_version={FV}
               GROUP BY net ORDER BY n DESC"""):
    print(f"    net={x['net']:<12} {x['n']}")

print("\n=== 6. the claimed 'model-eligible blocked' set, re-derived with"
      " asset_class + buildability ===")
cand = q(f"""SELECT o.kind, o.key, COALESCE(o.network_id,'') net, o.entry_ts,
                    o.status,
                    e.id AS ev_id, e.ticker, e.price_usd, e.market_cap,
                    (SELECT MAX(r.feature_version) FROM training_rows r
                       WHERE r.kind=o.kind AND r.key=o.key) AS have_fv
             FROM outcomes o
             LEFT JOIN signal_events e ON e.id = o.key
             WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
               AND o.entry_ts >= ?
               AND NOT EXISTS (SELECT 1 FROM training_rows r
                    WHERE r.kind='signal' AND r.key=o.key
                      AND r.feature_version=?)""",
         (config.LIVE_START_TS, features.FEATURE_VERSION))
print(f"  raw candidate count (claimant's population) = {len(cand)}")
import collections
by_net = collections.Counter(c["net"] for c in cand)
print("  by net:", dict(by_net.most_common()))
no_event = [c for c in cand if c["ev_id"] is None]
print(f"  UNBUILDABLE (no signal_events row -> build_training_row returns None)"
      f" = {len(no_event)}")
ac = collections.Counter()
ac_net = collections.Counter()
for c in cand:
    if c["ev_id"] is None:
        ac["<no event>"] += 1
        continue
    sym, px, mc = c["ticker"], c["price_usd"], c["market_cap"]
    klass = None
    if sym or px is not None or mc is not None:
        klass = classify_asset(sym, px, px, mc)[0]
    ac[klass or "<NULL>"] += 1
    if klass == "meme":
        ac_net[c["net"]] += 1
print("  asset_class the builder WOULD write:", dict(ac.most_common()))
print("  meme-only, by net:", dict(ac_net.most_common()))
meme_evm = sum(n for net, n in ac_net.items() if net in evm)
meme_other = sum(n for net, n in ac_net.items() if net not in evm)
print(f"  => truly model-eligible AND blocked by the EVM filter = {meme_evm}")
print(f"  => truly model-eligible pending on NON-excluded nets  = {meme_other}")
have_stale = sum(1 for c in cand if c["have_fv"] is not None)
print(f"  of all candidates: {have_stale} already have some older-fv row,"
      f" {len(cand)-have_stale} have none")

print("\n=== 7. does the fv8 EVM cohort even carry live/meme rows? ===")
r = q("""SELECT COUNT(*) n FROM training_rows
         WHERE feature_version=8 AND kind='signal' AND is_live=1
           AND asset_class='meme' AND status='ok' AND is_independent=1""")[0]
print(f"  fv8 rows that would be model-eligible if refreshed = {r['n']}")

print("\n=== 8. sanity: is the builder alive and is fv12 growing? ===")
for r in q("""SELECT key, CAST(value AS TEXT) v FROM meta
              WHERE key IN ('build_rows_last_run_at','build_rows_last_ok_at',
                            'labeler_last_run_at','recorder_last_cycle_at')"""):
    print(f"  {r['key']} = {r['v']}")
for r in q("""SELECT substr(built_at,1,10) d, COUNT(*) n FROM training_rows
              WHERE feature_version=12 GROUP BY d ORDER BY d DESC LIMIT 12"""):
    print(f"  fv12 built_at {r['d']}: {r['n']}")

print("\n=== 9. EVM ledger repair progress (is the interlock waiting on work?) ===")
for t in ("evm_replay_state", "evm_backfill_state", "chain_fetch_state"):
    try:
        rows = q(f"SELECT COUNT(*) n FROM {t}")
        print(f"  {t}: {rows[0]['n']} rows")
    except sqlite3.Error as e:
        print(f"  {t}: ERROR {e}")
con.close()
