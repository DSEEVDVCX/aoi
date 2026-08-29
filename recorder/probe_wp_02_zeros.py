import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()):
    return con.execute(sql, args).fetchall()

print("=== 1. token_static: description_len (extract.py:448  len(desc) if desc else 0) ===")
r = q("""SELECT COUNT(*) total,
                SUM(CASE WHEN description IS NULL THEN 1 ELSE 0 END) desc_null,
                SUM(CASE WHEN description_len = 0 THEN 1 ELSE 0 END) len_zero,
                SUM(CASE WHEN description IS NULL AND description_len = 0 THEN 1 ELSE 0 END) both,
                SUM(CASE WHEN description_len IS NULL THEN 1 ELSE 0 END) len_null,
                SUM(CASE WHEN description IS NOT NULL AND description_len = 0 THEN 1 ELSE 0 END) real_zero
           FROM token_static""")[0]
print(dict(r))

print("\n=== 1b. same in distinct tokens ===")
r = q("""SELECT COUNT(*) toks,
                SUM(CASE WHEN d IS NULL THEN 1 ELSE 0 END) desc_null
           FROM (SELECT token_address, network_id, MAX(description) d FROM token_static
                  GROUP BY token_address, network_id)""")[0]
print(dict(r))

print("\n=== 2. token_static: has_banner / has_image / cmc_id ===")
r = q("""SELECT COUNT(*) total,
                SUM(CASE WHEN has_banner=0 THEN 1 ELSE 0 END) banner0,
                SUM(CASE WHEN has_banner IS NULL THEN 1 ELSE 0 END) bannernull,
                SUM(CASE WHEN has_image=0 THEN 1 ELSE 0 END) img0,
                SUM(CASE WHEN cmc_id IS NULL THEN 1 ELSE 0 END) cmcnull,
                SUM(CASE WHEN exchanges_count IS NULL THEN 1 ELSE 0 END) exnull,
                SUM(CASE WHEN description IS NULL AND has_image=0 AND has_banner=0
                          AND cmc_id IS NULL THEN 1 ELSE 0 END) info_block_looks_absent
           FROM token_static""")[0]
print(dict(r))

print("\n=== 3. token_holders hodlers_top: platform_holders_listed (extract.py:1044 len() or None) ===")
r = q("""SELECT COUNT(*) total,
                SUM(CASE WHEN platform_holders_listed IS NULL THEN 1 ELSE 0 END) listed_null,
                SUM(CASE WHEN platform_holders_listed IS NULL AND platform_holders IS NOT NULL
                         THEN 1 ELSE 0 END) listed_null_but_total_known,
                SUM(CASE WHEN platform_holders = 0 THEN 1 ELSE 0 END) total_zero,
                SUM(CASE WHEN platform_holders_listed = 0 THEN 1 ELSE 0 END) listed_zero,
                SUM(CASE WHEN platform_value_usd IS NULL THEN 1 ELSE 0 END) value_null,
                SUM(CASE WHEN platform_underwater IS NULL THEN 1 ELSE 0 END) uw_null,
                SUM(CASE WHEN platform_dev_holding IS NULL THEN 1 ELSE 0 END) dev_null
           FROM token_holders WHERE source='hodlers_top'""")[0]
print(dict(r))

print("\n=== 4. training_rows population: fabricated-zero suspects ===")
FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme'
          AND status='ok' AND is_independent=1 AND feature_version = {FV}"""
for col in ("description_len", "has_cmc_id", "has_banner", "has_twitter",
            "socials_count", "ath_history_complete", "listed_on_exchange",
            "exchanges_count"):
    r = q(f"""SELECT COUNT(*) t,
                     SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) nulls,
                     SUM(CASE WHEN {col}=0 THEN 1 ELSE 0 END) zeros,
                     SUM(CASE WHEN {col}>0 THEN 1 ELSE 0 END) pos {POP}""")[0]
    print(f"{col:24s} n={r['t']} null={r['nulls']} zero={r['zeros']} pos={r['pos']}")

print("\n=== 5. ath_history_complete=0 vs dist_from_ath / bars ===")
r = q(f"""SELECT SUM(CASE WHEN ath_history_complete=0 THEN 1 ELSE 0 END) c0,
                 SUM(CASE WHEN ath_history_complete=0 AND dist_from_ath IS NULL THEN 1 ELSE 0 END) c0_noath,
                 SUM(CASE WHEN ath_history_complete=0 AND bars_count_24h IS NULL THEN 1 ELSE 0 END) c0_nobars,
                 SUM(CASE WHEN ath_history_complete=1 THEN 1 ELSE 0 END) c1,
                 SUM(CASE WHEN ath_history_complete IS NULL THEN 1 ELSE 0 END) cnull {POP}""")[0]
print(dict(r))
con.close()
