import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

POP = """
  FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1
   AND feature_version = CAST(COALESCE((SELECT value FROM meta
                          WHERE key='current_feature_version'),'0') AS INTEGER)
"""

print("== population sizes ==")
r = con.execute("SELECT COUNT(*) n" + POP).fetchone()
print("model population n =", r["n"])
n = r["n"]

# my own definition of "flow covered": any raw flow component present,
# NOT flow_age_min (deliberately a different marker than theirs)
r = con.execute("""
SELECT
  SUM(CASE WHEN flow_buy_volume_5m IS NOT NULL OR flow_sell_volume_5m IS NOT NULL
            OR flow_buy_count_5m IS NOT NULL OR flow_sell_count_5m IS NOT NULL
           THEN 1 ELSE 0 END) any_raw_flow,
  SUM(CASE WHEN flow_age_min IS NOT NULL THEN 1 ELSE 0 END) has_age,
  SUM(CASE WHEN flow_age_min IS NOT NULL
            AND flow_buy_volume_5m IS NULL AND flow_sell_volume_5m IS NULL
            AND flow_buy_count_5m IS NULL AND flow_sell_count_5m IS NULL
           THEN 1 ELSE 0 END) age_but_no_raw
""" + POP).fetchone()
print("any_raw_flow =", r["any_raw_flow"], " has_flow_age_min =", r["has_age"],
      " age_but_all_raw_null =", r["age_but_no_raw"])

print()
print("== per-ratio 3-way split (only rows where flow is covered) ==")
FLOWPOP = POP + " AND flow_age_min IS NOT NULL"
for ratio, num, den in (
    ("flow_buy_sell_volume_ratio_5m", "flow_buy_volume_5m", "flow_sell_volume_5m"),
    ("flow_buy_sell_volume_ratio_1h", None, None),
    ("flow_buy_sell_count_ratio_5m", "flow_buy_count_5m", "flow_sell_count_5m"),
    ("flow_unique_ratio_5m", "flow_unique_buys_5m", "flow_buy_count_5m"),
    ("flow_trade_size_5m", "flow_buy_volume_5m", "flow_buy_count_5m"),
):
    if num is None:
        continue
    q = f"""
SELECT COUNT(*) covered,
       SUM(CASE WHEN {ratio} IS NOT NULL THEN 1 ELSE 0 END) ratio_ok,
       SUM(CASE WHEN {ratio} IS NULL AND {den}=0 AND {num} IS NOT NULL THEN 1 ELSE 0 END) zero_den_num_ok,
       SUM(CASE WHEN {ratio} IS NULL AND {den}=0 AND {num} IS NULL THEN 1 ELSE 0 END) zero_den_num_null,
       SUM(CASE WHEN {ratio} IS NULL AND {den} IS NULL THEN 1 ELSE 0 END) den_null,
       SUM(CASE WHEN {ratio} IS NULL AND {den} IS NOT NULL AND {den}<>0 THEN 1 ELSE 0 END) other,
       SUM(CASE WHEN {ratio} IS NULL AND {den}=0 AND {num}=0 THEN 1 ELSE 0 END) both_zero
    {FLOWPOP}"""
    row = con.execute(q).fetchone()
    print(f"{ratio}: covered={row['covered']} ok={row['ratio_ok']} "
          f"zeroden_num_present={row['zero_den_num_ok']} "
          f"(of which num==0: {row['both_zero']}) "
          f"zeroden_num_null={row['zero_den_num_null']} "
          f"den_null={row['den_null']} other={row['other']}")

print()
print("== recoverability: can a consumer tell 'zero denominator' from 'no flow'? ==")
r = con.execute("""
SELECT
  SUM(CASE WHEN flow_buy_sell_volume_ratio_5m IS NULL AND flow_sell_volume_5m=0
            AND flow_buy_volume_5m IS NOT NULL AND flow_age_min IS NOT NULL
           THEN 1 ELSE 0 END) distinguishable,
  SUM(CASE WHEN flow_buy_sell_volume_ratio_5m IS NULL AND flow_sell_volume_5m=0
            AND flow_buy_volume_5m IS NOT NULL AND flow_age_min IS NULL
           THEN 1 ELSE 0 END) not_distinguishable
""" + POP).fetchone()
print("vol5m collapsed rows where raw cols ARE present in same row:", r["distinguishable"])
print("vol5m collapsed rows where raw cols are NOT present:", r["not_distinguishable"])

print()
print("== social_holder_ratio ==")
r = con.execute("""
SELECT SUM(CASE WHEN social_thesis_authors IS NOT NULL THEN 1 ELSE 0 END) has_social,
       SUM(CASE WHEN social_holder_ratio IS NULL AND social_thesis_authors=0
                 AND social_holder_authors IS NOT NULL THEN 1 ELSE 0 END) collapsed,
       SUM(CASE WHEN social_holder_ratio IS NULL AND social_thesis_authors=0
                 AND social_holder_authors=0 THEN 1 ELSE 0 END) both_zero,
       SUM(CASE WHEN social_thesis_authors=0 THEN 1 ELSE 0 END) den_zero_all
""" + POP).fetchone()
print(dict(r))

print()
print("== platform_underwater_ratio: which denominator? ==")
cols = {c[1] for c in con.execute("PRAGMA table_info(training_rows)").fetchall()}
print("platform_holders_listed in training_rows:", "platform_holders_listed" in cols)
print("platform_underwater in training_rows:", "platform_underwater" in cols)
r = con.execute("""
SELECT SUM(CASE WHEN platform_holders IS NOT NULL THEN 1 ELSE 0 END) has_plat,
       SUM(CASE WHEN platform_holders=0 THEN 1 ELSE 0 END) plat_zero,
       SUM(CASE WHEN platform_underwater_ratio IS NULL AND platform_holders=0
                THEN 1 ELSE 0 END) claim_case,
       SUM(CASE WHEN platform_underwater_ratio IS NULL AND platform_holders IS NOT NULL
                THEN 1 ELSE 0 END) null_ratio_with_plat
""" + POP).fetchone()
print(dict(r))
con.close()
