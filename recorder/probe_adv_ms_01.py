import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

fv = con.execute(
    "SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
).fetchone()[0]
print("current_feature_version =", fv)

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1 AND feature_version=?"""

n = con.execute(f"SELECT COUNT(*) FROM training_rows WHERE {POP}", (fv,)).fetchone()[0]
print("population rows =", n)

# my own decisive single-column tests (not the 21-col AND)
for col in ("tick_age_min", "liquidity", "volume_24h", "tick_change_24h",
            "holders", "top10_holders_pct", "tick_rich_age_min", "float_ratio",
            "token_age_h", "decimals", "socials_count", "launchpad_name",
            "exchanges_count", "has_twitter", "creator_prior_tokens"):
    k = con.execute(
        f"SELECT COUNT(*) FROM training_rows WHERE {POP} AND {col} IS NULL", (fv,)
    ).fetchone()[0]
    print(f"  {col:22s} NULL in {k:6d} / {n} = {100.0*k/n:5.2f}%")

# their exact 21-col AND, for agreement check
MS = ["liquidity","holders","volume_24h","tick_age_min","tick_change_1h",
      "tick_volume_1h","tick_txn_1h","top10_holders_pct","buy_count_24h",
      "sell_count_24h","buy_sell_ratio_24h","unique_buys_24h","unique_sells_24h",
      "tick_change_4h","tick_change_24h","tick_volume_4h","tick_txn_24h",
      "volume_to_liquidity","liquidity_to_mcap","float_ratio","tick_rich_age_min"]
q = f"SELECT COUNT(*) FROM training_rows WHERE {POP} AND " + " AND ".join(
    f"{c} IS NULL" for c in MS)
print("theirs market_snapshot all-NULL =", con.execute(q, (fv,)).fetchone()[0])

TS = ["token_age_h","launchpad_name","migrated","graduation_percent","is_scam",
      "mintable","freezable","socials_count","has_twitter","creator_prior_tokens",
      "decimals","name_len","name_non_ascii","exchanges_count",
      "listed_on_exchange","has_cmc_id","description_len","has_banner"]
q2 = f"SELECT COUNT(*) FROM training_rows WHERE {POP} AND " + " AND ".join(
    f"{c} IS NULL" for c in TS)
print("theirs token_static all-NULL =", con.execute(q2, (fv,)).fetchone()[0])

# how many rows have tick_age_min NULL but some other market col non-null
q3 = f"""SELECT COUNT(*) FROM training_rows WHERE {POP} AND tick_age_min IS NULL
         AND (liquidity IS NOT NULL OR volume_24h IS NOT NULL
              OR holders IS NOT NULL OR tick_change_24h IS NOT NULL)"""
print("tick_age_min NULL yet other market col present =",
      con.execute(q3, (fv,)).fetchone()[0])
con.close()
