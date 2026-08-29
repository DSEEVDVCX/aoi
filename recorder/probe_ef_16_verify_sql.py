import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
     AND is_independent=1 AND feature_version = CAST(COALESCE(
       (SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""

Q = {}

Q["F1 blocked EVM candidates"] = f"""
SELECT o.network_id, COUNT(*) n, date(MIN(o.entry_ts),'unixepoch') first_d,
       date(MAX(o.entry_ts),'unixepoch') last_d
  FROM outcomes o
 WHERE o.kind='signal' AND o.status='ok' AND o.is_independent=1
   AND o.entry_ts >= {config.LIVE_START_TS}
   AND COALESCE(o.network_id,'') IN ('4663','8453','143')
   AND NOT EXISTS (SELECT 1 FROM training_rows r
                    WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)
 GROUP BY 1"""

Q["F1b stale fv8"] = """
SELECT network_id, feature_version, is_independent, COUNT(*) n
  FROM training_rows WHERE kind='signal' AND feature_version<>12
 GROUP BY 1,2,3 ORDER BY n DESC"""

Q["F1c gate flags"] = """
SELECT key, value FROM meta
 WHERE key IN ('evm_ledger_rebuild_required','evm_training_rebuild_started',
               'current_feature_version')"""

Q["F2 reachable-but-NULL onchain_conc"] = f"""
SELECT COUNT(*) n, COUNT(DISTINCT t.token_address) tokens,
       MIN(date(t.entry_ts,'unixepoch')) a, MAX(date(t.entry_ts,'unixepoch')) b
  FROM training_rows t
 WHERE {POP}
   AND t.onchain_top1_pct IS NULL AND t.onchain_top5_pct IS NULL
   AND t.onchain_top10_pct IS NULL AND t.onchain_top20_pct IS NULL
   AND t.onchain_top_accounts IS NULL AND t.onchain_age_min IS NULL
   AND t.onchain_top1_delta_5m IS NULL AND t.onchain_top10_delta_5m IS NULL
   AND t.onchain_delta_span_min IS NULL AND t.onchain_holder_count IS NULL
   AND t.onchain_holders_delta_5m IS NULL
   AND EXISTS (SELECT 1 FROM chain_concentration c
                WHERE c.token_address=t.token_address
                  AND c.network_id=t.network_id
                  AND c.top1_pct IS NOT NULL
                  AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= t.entry_ts)"""

Q["F3 onchain_contract_evm all NULL"] = f"""
SELECT COUNT(*) total,
       SUM(CASE WHEN onchain_code_size IS NULL AND onchain_function_count IS NULL
                 AND onchain_is_proxy IS NULL AND onchain_owner_renounced IS NULL
                 AND onchain_has_mint_fn IS NULL AND onchain_has_pause_fn IS NULL
                 AND onchain_has_blacklist_fn IS NULL AND onchain_has_fee_setter IS NULL
                 AND onchain_has_limit_setter IS NULL AND onchain_has_trading_switch IS NULL
                 AND onchain_contract_age_min IS NULL THEN 1 ELSE 0 END) all_null,
       SUM(CASE WHEN network_id='8453' THEN 1 ELSE 0 END) base_rows,
       MAX(CASE WHEN network_id='8453' THEN date(entry_ts,'unixepoch') END) last_base_entry
  FROM training_rows WHERE {POP}"""

Q["F4 2026-08-19 hole"] = f"""
SELECT date(entry_ts,'unixepoch') d, COUNT(*) n,
       ROUND(100.0*SUM(CASE WHEN liquidity IS NULL AND holders IS NULL
             AND volume_24h IS NULL AND tick_age_min IS NULL
             AND tick_change_1h IS NULL AND tick_volume_1h IS NULL
             AND tick_txn_1h IS NULL AND top10_holders_pct IS NULL
             AND buy_count_24h IS NULL AND sell_count_24h IS NULL
             AND buy_sell_ratio_24h IS NULL AND unique_buys_24h IS NULL
             AND unique_sells_24h IS NULL AND tick_change_4h IS NULL
             AND tick_change_24h IS NULL AND tick_volume_4h IS NULL
             AND tick_txn_24h IS NULL AND volume_to_liquidity IS NULL
             AND liquidity_to_mcap IS NULL AND float_ratio IS NULL
             AND tick_rich_age_min IS NULL THEN 1 ELSE 0 END)/COUNT(*),1) pct_mktsnap_null
  FROM training_rows WHERE {POP}
   AND entry_ts >= strftime('%s','2026-08-17')
 GROUP BY 1 ORDER BY 1"""

Q["M1 network mix of population"] = f"""
SELECT network_id, COUNT(*) n, date(MIN(entry_ts),'unixepoch') a,
       date(MAX(entry_ts),'unixepoch') b
  FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY n DESC"""

for lbl, q in Q.items():
    print("=" * 70)
    print(lbl)
    try:
        for r in con.execute(q):
            print("   ", dict(r))
    except Exception as e:
        print("   FAILED:", e)
con.close()
