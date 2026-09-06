"""Final: per-network coverage of the onchain families, stale fv=8 rows, all-empty-family rows."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row
MW = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1
        AND feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""

print("-- onchain families by network, MODEL POPULATION (non-null counts) --")
q = f"""SELECT network_id, COUNT(*) n,
   SUM(onchain_holder_count IS NOT NULL) holder_count,
   SUM(onchain_holders_delta_5m IS NOT NULL) delta5m,
   SUM(onchain_top10_pct IS NOT NULL) top10,
   SUM(onchain_has_mint_authority IS NOT NULL) mint_auth,
   SUM(onchain_code_size IS NOT NULL) code_size,
   SUM(flow_buy_volume_5m IS NOT NULL) flow5m,
   SUM(chain_holder_count IS NOT NULL) chain_hc,
   SUM(platform_holders IS NOT NULL) plat_h,
   SUM(social_thesis_total IS NOT NULL) social
  FROM training_rows WHERE {MW} GROUP BY 1 ORDER BY n DESC"""
for r in con.execute(q):
    print("   ", dict(r))

print("\n-- stale feature_version=8 rows by kind/network --")
for r in con.execute("""SELECT kind, network_id, COUNT(*) n FROM training_rows
                         WHERE feature_version=8 GROUP BY 1,2 ORDER BY n DESC"""):
    print("   ", dict(r))
r = con.execute("SELECT COUNT(*) n FROM training_rows WHERE feature_version<>12").fetchone()
t = con.execute("SELECT COUNT(*) n FROM training_rows").fetchone()
print(f"   stale rows: {r['n']} of {t['n']} = {100*r['n']/t['n']:.2f}%")

print("\n-- model rows whose ENTIRE enrichment family is NULL (a 'row of nothing') --")
q2 = f"""SELECT
  SUM(CASE WHEN flow_buy_volume_5m IS NULL AND flow_net_volume_1h IS NULL
                AND flow_buy_sell_volume_ratio_24h IS NULL THEN 1 ELSE 0 END) no_flow,
  SUM(CASE WHEN chain_holder_count IS NULL AND platform_holders IS NULL
                AND chain_top10_pct IS NULL THEN 1 ELSE 0 END) no_holders,
  SUM(CASE WHEN onchain_top10_pct IS NULL AND onchain_has_mint_authority IS NULL
                AND onchain_holder_count IS NULL AND onchain_code_size IS NULL THEN 1 ELSE 0 END) no_onchain,
  SUM(CASE WHEN social_thesis_total IS NULL AND thesis_counted=0 THEN 1 ELSE 0 END) no_social,
  SUM(CASE WHEN token_age_h IS NULL AND launchpad_name IS NULL AND decimals IS NULL THEN 1 ELSE 0 END) no_static,
  SUM(CASE WHEN liquidity IS NULL AND volume_24h IS NULL AND holders IS NULL THEN 1 ELSE 0 END) no_market,
  COUNT(*) n
 FROM training_rows WHERE {MW}"""
r = con.execute(q2).fetchone()
print("   ", dict(r))

print("\n-- model rows missing flow AND holders AND onchain (all three enrichment families) --")
q3 = f"""SELECT COUNT(*) n FROM training_rows WHERE {MW}
   AND flow_buy_volume_5m IS NULL AND flow_net_volume_1h IS NULL
   AND chain_holder_count IS NULL AND platform_holders IS NULL
   AND onchain_top10_pct IS NULL AND onchain_has_mint_authority IS NULL
   AND onchain_holder_count IS NULL"""
print("   ", con.execute(q3).fetchone()["n"], "of", con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {MW}").fetchone()["n"])
