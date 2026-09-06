"""What blocks the 12 NULL columns and the EVM contract family, measured."""
from __future__ import annotations

import os
import sqlite3
import sys

import config

sys.stdout.reconfigure(encoding="utf-8")
URI = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(URI, uri=True, timeout=180)
con.row_factory = sqlite3.Row
q = lambda s, *a: con.execute(s, a).fetchall()

print("=== evm_contract: what the collector has actually gathered ===")
for r in q("""SELECT network_id, COUNT(*) rows, COUNT(DISTINCT token_address) toks,
                     MIN(recorded_at) lo, MAX(recorded_at) hi
                FROM evm_contract GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net {r['network_id']:>12}  rows {r['rows']:>7}  tokens {r['toks']:>5}  {r['lo'][:16]} .. {r['hi'][:16]}")

print("\n=== variance ALREADY observable in evm_contract (Base) ===")
cols = ["is_proxy", "owner_renounced", "has_mint_fn", "has_pause_fn",
        "has_blacklist_fn", "has_fee_setter", "has_limit_setter",
        "has_trading_switch", "code_size", "function_count"]
have = {r["name"] for r in q("PRAGMA table_info(evm_contract)")}
for c in cols:
    if c not in have:
        print(f"  {c:22} (no such column)")
        continue
    r = q(f"""SELECT COUNT(*) n, COUNT({c}) nn, COUNT(DISTINCT {c}) d,
                     MIN({c}) lo, MAX({c}) hi FROM evm_contract""")[0]
    print(f"  {c:22} nonnull {r['nn']:>6}  distinct {r['d']:>4}  min {r['lo']}  max {r['hi']}")

print("\n=== model population by network (what widening would buy) ===")
FV = "(SELECT CAST(value AS INTEGER) FROM meta WHERE key='current_feature_version')"
POP = (f"kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")
tot = q(f"SELECT COUNT(*) n FROM training_rows WHERE {POP}")[0]["n"]
for r in q(f"""SELECT COALESCE(network_id,'') net, COUNT(*) n,
                      MAX(entry_ts) mx FROM training_rows WHERE {POP}
                GROUP BY 1 ORDER BY 2 DESC"""):
    import datetime as dt
    mx = dt.datetime.utcfromtimestamp(r["mx"]).strftime("%Y-%m-%d")
    print(f"  net {r['net']:>12}  {r['n']:>6} rows ({100.0*r['n']/tot:5.1f}%)  newest entry {mx}")
print(f"  TOTAL {tot}")

print("\n=== the EVM training quarantine ===")
for k in ("evm_ledger_rebuild_required", "evm_training_rebuild_started",
          "evm_ledger_generation"):
    v = q("SELECT value FROM meta WHERE key=?", k)
    print(f"  meta.{k:34} = {v[0][0] if v else None}")
for r in q("""SELECT network_id, COALESCE(status,'(null)') st, COUNT(*) n
                FROM evm_backfill_state GROUP BY 1,2 ORDER BY 1,3 DESC"""):
    print(f"  backfill net {r['network_id']:>6}  {r['st']:<10} {r['n']:>5}")

print("\n=== top10_holders_pct vs its live replacements ===")
for c in ("top10_holders_pct", "chain_top10_pct", "onchain_top10_pct"):
    r = q(f"SELECT COUNT(*) n, COUNT({c}) nn, COUNT(DISTINCT {c}) d "
          f"FROM training_rows WHERE {POP}")[0]
    print(f"  {c:20} nonnull {r['nn']:>6} / {r['n']}  distinct {r['d']}")
r = q("SELECT COUNT(*) n, COUNT(top10_holders_pct) nn FROM market_ticks")[0]
print(f"  market_ticks.top10_holders_pct nonnull {r['nn']} / {r['n']}")
con.close()
