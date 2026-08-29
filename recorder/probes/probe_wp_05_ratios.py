import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== 1) why is the EVM-contract family 100% NULL? (config.EVM_CONTRACT_NETWORKS=('8453',)) ===")
for r in q(f"SELECT network_id, COUNT(*) c FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY c DESC"):
    print(f"   model rows net={r['network_id']:<12} {r['c']}")
r = q("""SELECT COUNT(*) c, MIN(recorded_at) lo, MAX(recorded_at) hi,
                COUNT(DISTINCT token_address) tok, network_id
           FROM evm_contract GROUP BY network_id""")
for x in r:
    print(f"   evm_contract net={x['network_id']} rows={x['c']} tokens={x['tok']} {x['lo']} .. {x['hi']}")
r = q(f"""SELECT COUNT(*) c, MAX(datetime(entry_ts,'unixepoch')) latest_t0
            FROM training_rows WHERE {POP} AND network_id='8453'""")[0]
print(f"   Base (8453) model rows={r['c']} latest entry_ts={r['latest_t0']}")
r = q(f"""SELECT COUNT(*) c FROM training_rows t WHERE {POP} AND network_id='8453'
            AND EXISTS (SELECT 1 FROM evm_contract e
                        WHERE lower(e.token_address)=lower(t.token_address)
                          AND e.network_id=t.network_id)""")[0]
print(f"   Base model rows whose token appears in evm_contract at ANY time: {r['c']}")

print()
print("=== 2) token_static.token_created_at format — recorder.token_age_days() cannot parse ISO ===")
r = q("""SELECT COUNT(*) tot,
                SUM(CASE WHEN token_created_at IS NULL OR token_created_at='' THEN 1 ELSE 0 END) empty,
                SUM(CASE WHEN token_created_at GLOB '*[0-9]' AND token_created_at NOT GLOB '*[^0-9.]*'
                         THEN 1 ELSE 0 END) numeric_ok,
                SUM(CASE WHEN token_created_at GLOB '*[^0-9.]*' THEN 1 ELSE 0 END) non_numeric
           FROM token_static""")[0]
print(f"   token_static rows={r['tot']} empty={r['empty']} pure-numeric={r['numeric_ok']} contains-non-digit={r['non_numeric']}")
for x in q("""SELECT token_created_at v, COUNT(*) c FROM token_static
               WHERE token_created_at GLOB '*[^0-9.]*' GROUP BY 1 ORDER BY c DESC LIMIT 5"""):
    print(f"      sample non-numeric value: {x['v']!r}  x{x['c']}")

print()
print("=== 3) ratio features: how often does a MEASURED zero denominator become NULL? ===")
tests = [
  ("buy_sell_ratio_24h",  "buy_count_24h IS NOT NULL AND sell_count_24h=0"),
  ("volume_to_liquidity", "volume_24h IS NOT NULL AND liquidity=0"),
  ("liquidity_to_mcap",   "liquidity IS NOT NULL AND market_cap=0"),
  ("float_ratio",         "float_ratio IS NULL AND total_supply_is_zero"),
  ("size_to_mcap",        "size_usd IS NOT NULL AND market_cap=0"),
  ("log_market_cap",      "market_cap=0"),
  ("log_size_usd",        "size_usd=0"),
  ("volume_per_trader",   "total_volume IS NOT NULL AND unique_traders=0"),
  ("price_to_avg_cost",   "price_usd IS NOT NULL AND avg_cost=0"),
  ("vol_surge_1h",        "bar_vol_1h IS NOT NULL AND bar_vol_24h=0"),
  ("thesis_accel",        "thesis_1h IS NOT NULL AND thesis_24h=0"),
  ("social_holder_ratio", "social_holder_authors IS NOT NULL AND social_thesis_authors=0"),
  ("platform_penetration","platform_holders IS NOT NULL AND chain_holder_count=0"),
  ("top_trader_match_ratio","top_trader_match_count IS NOT NULL AND top_traders_listed=0"),
  ("flow_buy_sell_volume_ratio_5m","flow_buy_volume_5m IS NOT NULL AND flow_sell_volume_5m=0"),
  ("flow_buy_sell_count_ratio_5m","flow_buy_count_5m IS NOT NULL AND flow_sell_count_5m=0"),
  ("flow_unique_ratio_5m","flow_unique_buys_5m IS NOT NULL AND flow_buy_count_5m=0"),
  ("flow_trade_size_5m",  "flow_buy_volume_5m IS NOT NULL AND flow_buy_count_5m=0"),
]
tot = q(f"SELECT COUNT(*) c FROM training_rows WHERE {POP}")[0]["c"]
for name, cond in tests:
    if "total_supply_is_zero" in cond:
        continue
    try:
        r = q(f"""SELECT SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) hit,
                         SUM(CASE WHEN ({cond}) AND {name} IS NULL THEN 1 ELSE 0 END) nulled,
                         SUM(CASE WHEN {name} IS NOT NULL THEN 1 ELSE 0 END) nonnull
                    FROM training_rows WHERE {POP}""")[0]
        print(f"   {name:32s} zero-denominator rows={r['hit'] or 0:<6} of which NULL={r['nulled'] or 0:<6}"
              f" | col non-null overall={r['nonnull']}/{tot}")
    except Exception as e:
        print(f"   {name:32s} ERR {e}")

print()
print("=== 4) static-family fabricated zeros (description_len / has_cmc_id / socials_count / has_twitter) ===")
r = q("""SELECT COUNT(*) tot,
                SUM(CASE WHEN description IS NULL THEN 1 ELSE 0 END) desc_null,
                SUM(CASE WHEN description IS NULL AND description_len=0 THEN 1 ELSE 0 END) desc_null_len0,
                SUM(CASE WHEN description_len IS NULL THEN 1 ELSE 0 END) len_null,
                SUM(CASE WHEN cmc_id IS NULL THEN 1 ELSE 0 END) cmc_null,
                SUM(CASE WHEN exchanges_count IS NULL THEN 1 ELSE 0 END) exch_null,
                SUM(CASE WHEN twitter IS NULL AND telegram IS NULL AND website IS NULL
                          AND discord IS NULL THEN 1 ELSE 0 END) no_socials_at_all
           FROM token_static""")[0]
print(f"   token_static rows={r['tot']}")
print(f"   description IS NULL={r['desc_null']}  of which description_len=0 -> {r['desc_null_len0']}"
      f"   description_len IS NULL -> {r['len_null']}")
print(f"   cmc_id IS NULL={r['cmc_null']}  exchanges_count IS NULL={r['exch_null']}"
      f"  all four social links NULL={r['no_socials_at_all']}")
r = q(f"""SELECT COUNT(*) tot,
                 SUM(CASE WHEN description_len=0 THEN 1 ELSE 0 END) d0,
                 SUM(CASE WHEN description_len IS NULL THEN 1 ELSE 0 END) dN,
                 SUM(CASE WHEN has_cmc_id=0 THEN 1 ELSE 0 END) c0,
                 SUM(CASE WHEN has_cmc_id IS NULL THEN 1 ELSE 0 END) cN,
                 SUM(CASE WHEN socials_count=0 THEN 1 ELSE 0 END) s0,
                 SUM(CASE WHEN socials_count IS NULL THEN 1 ELSE 0 END) sN,
                 SUM(CASE WHEN exchanges_count IS NULL THEN 1 ELSE 0 END) eN,
                 SUM(CASE WHEN listed_on_exchange IS NULL THEN 1 ELSE 0 END) lN
            FROM training_rows WHERE {POP}""")[0]
print(f"   model rows={r['tot']}: description_len=0 -> {r['d0']} (NULL {r['dN']}) |"
      f" has_cmc_id=0 -> {r['c0']} (NULL {r['cN']}) | socials_count=0 -> {r['s0']} (NULL {r['sN']})")
print(f"   exchanges_count NULL -> {r['eN']}  listed_on_exchange NULL -> {r['lN']}  (correctly guarded pair)")

print()
print("=== 5) top_traders_listed = 0 by signal_type (empty list fabricated for large_buy) ===")
for x in q(f"""SELECT signal_type, COUNT(*) c,
                      SUM(CASE WHEN top_traders_listed=0 THEN 1 ELSE 0 END) z,
                      SUM(CASE WHEN top_traders_listed IS NULL THEN 1 ELSE 0 END) n
                 FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY c DESC"""):
    print(f"   {x['signal_type']:<18} rows={x['c']:<6} top_traders_listed=0 -> {x['z']:<6} NULL -> {x['n']}")

con.close()
