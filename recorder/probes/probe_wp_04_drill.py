import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
W = f"""kind='signal' AND is_live=1 AND asset_class='meme'
        AND status='ok' AND is_independent=1 AND feature_version = {FV}"""
def one(sql, args=()):
    return con.execute(sql, args).fetchone()
def all_(sql, args=()):
    return con.execute(sql, args).fetchall()

print("=== 1. token_flow source side: zero-denominator states in the 1h/24h tiers ===")
for p in ("5m", "1h", "4h", "24h"):
    try:
        r = one(f"""SELECT COUNT(*) n,
          SUM(CASE WHEN sell_volume_{p}=0 AND buy_volume_{p}>0 THEN 1 ELSE 0 END) sell0_buypos,
          SUM(CASE WHEN sell_volume_{p}=0 AND buy_volume_{p}=0 THEN 1 ELSE 0 END) both0,
          SUM(CASE WHEN sell_volume_{p} IS NULL THEN 1 ELSE 0 END) sellnull
          FROM token_flow""")
        print(f"  {p:4s} rows={r['n']} sell=0&buy>0 -> {r['sell0_buypos']}  both0={r['both0']} sellnull={r['sellnull']}")
    except Exception as e:
        print(f"  {p}: FAILED {e}")

print("\n=== 2. top_traders_listed: 0 vs top_trader_match_count, by signal_type ===")
for r in all_(f"""SELECT signal_type, COUNT(*) n,
   SUM(CASE WHEN top_traders_listed=0 THEN 1 ELSE 0 END) listed0,
   SUM(CASE WHEN top_traders_listed IS NULL THEN 1 ELSE 0 END) listednull,
   SUM(CASE WHEN top_traders_listed>0 THEN 1 ELSE 0 END) listedpos,
   SUM(CASE WHEN top_traders_listed=0 AND top_trader_match_count>0 THEN 1 ELSE 0 END) contradiction,
   SUM(CASE WHEN unique_traders IS NULL THEN 1 ELSE 0 END) ut_null,
   SUM(CASE WHEN total_volume IS NULL THEN 1 ELSE 0 END) tv_null
   FROM training_rows WHERE {W} GROUP BY signal_type ORDER BY n DESC"""):
    print("   ", dict(r))

print("\n=== 2b. signal_events raw: top_trader_ids_json shape by signal_type ===")
for r in all_("""SELECT signal_type, COUNT(*) n,
   SUM(CASE WHEN top_trader_ids_json IS NULL THEN 1 ELSE 0 END) json_null,
   SUM(CASE WHEN top_trader_ids_json='[]' THEN 1 ELSE 0 END) json_empty,
   SUM(CASE WHEN top_trader_ids_json NOT IN ('[]') AND top_trader_ids_json IS NOT NULL
            THEN 1 ELSE 0 END) json_nonempty,
   SUM(CASE WHEN unique_traders IS NULL THEN 1 ELSE 0 END) ut_null
   FROM signal_events GROUP BY signal_type ORDER BY n DESC LIMIT 10"""):
    print("   ", dict(r))

print("\n=== 3. thesis_counted = 0 : genuine zero or never-collected token? ===")
r = one(f"""SELECT COUNT(*) n,
   SUM(CASE WHEN thesis_counted=0 THEN 1 ELSE 0 END) tc0,
   SUM(CASE WHEN thesis_counted IS NULL THEN 1 ELSE 0 END) tcnull
   FROM training_rows WHERE {W}""")
print("  population:", dict(r))
r = one(f"""SELECT COUNT(*) n_rows,
   SUM(CASE WHEN has_any=0 THEN 1 ELSE 0 END) rows_token_never_collected
   FROM (SELECT t.rowid,
           (SELECT COUNT(*) FROM token_thesis th
             WHERE th.token_address=t.token_address AND th.network_id=t.network_id) has_any
           FROM training_rows t WHERE {W} AND thesis_counted=0)""")
print("  of the thesis_counted=0 rows:", dict(r))
r = one("""SELECT COUNT(DISTINCT token_address||'|'||network_id) toks FROM token_thesis""")
print("  distinct tokens present in token_thesis:", dict(r))
r = one(f"""SELECT COUNT(DISTINCT token_address||'|'||network_id) toks
            FROM training_rows WHERE {W}""")
print("  distinct tokens in population:", dict(r))

print("\n=== 4. platform_dev_holding / platform_holders in population ===")
r = one(f"""SELECT COUNT(*) n,
   SUM(CASE WHEN platform_holders=0 THEN 1 ELSE 0 END) ph0,
   SUM(CASE WHEN platform_holders=0 AND platform_dev_holding IS NULL THEN 1 ELSE 0 END) ph0_devnull,
   SUM(CASE WHEN platform_holders IS NULL THEN 1 ELSE 0 END) phnull,
   SUM(CASE WHEN platform_dev_holding IS NULL THEN 1 ELSE 0 END) devnull,
   SUM(CASE WHEN platform_value_usd IS NULL THEN 1 ELSE 0 END) valnull,
   SUM(CASE WHEN platform_underwater_ratio IS NULL THEN 1 ELSE 0 END) uwnull
   FROM training_rows WHERE {W}""")
print("  ", dict(r))

print("\n=== 5. exchanges_count distribution (listed_on_exchange constant?) ===")
for r in all_("""SELECT exchanges_count, COUNT(*) n FROM token_static
                  GROUP BY exchanges_count ORDER BY exchanges_count LIMIT 12"""):
    print("   ", dict(r))
print("  sample exchanges_json:", [x[0] for x in con.execute(
    "SELECT exchanges_json FROM token_static WHERE exchanges_count=1 LIMIT 3")])

print("\n=== 6. size_usd negatives ===")
r = one(f"""SELECT MIN(size_usd) mn, MAX(size_usd) mx,
   SUM(CASE WHEN size_usd<0 THEN 1 ELSE 0 END) neg FROM training_rows WHERE {W}""")
print("  ", dict(r))
for r in all_(f"""SELECT signal_type, COUNT(*) n FROM training_rows
                  WHERE {W} AND size_usd<0 GROUP BY signal_type"""):
    print("   neg by type:", dict(r))
for r in all_(f"""SELECT signal_type, COUNT(*) n FROM training_rows
                  WHERE {W} AND size_usd=0 GROUP BY signal_type"""):
    print("   zero by type:", dict(r))
con.close()
