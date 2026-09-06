import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1 AND feature_version = {FV}"""

print("current_feature_version:",
      con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

q = f"""SELECT COUNT(*) total,
 SUM(socials_count IS NULL) sc_null,
 SUM(has_twitter IS NULL) tw_null,
 SUM(socials_count=0) sc_zero,
 SUM(has_twitter=0) tw_zero,
 SUM(socials_count=0 AND has_twitter=0) both_zero,
 SUM(socials_count>0) sc_pos,
 SUM(has_twitter=1) tw_one,
 COUNT(DISTINCT token_address||'|'||network_id) toks
 {POP}"""
r = con.execute(q).fetchone()
print("MODEL POP:", dict(r))

print("\nsocials_count distribution (model pop):")
for row in con.execute(f"SELECT socials_count sc, COUNT(*) n, COUNT(DISTINCT token_address||'|'||network_id) t {POP} GROUP BY sc ORDER BY sc"):
    print("  sc=", row["sc"], "rows=", row["n"], "tokens=", row["t"])

print("\ncross-tab socials_count x has_twitter:")
for row in con.execute(f"SELECT socials_count sc, has_twitter tw, COUNT(*) n {POP} GROUP BY sc,tw ORDER BY sc,tw"):
    print("  sc=", row["sc"], "tw=", row["tw"], "n=", row["n"])

# whole training_rows for contrast (population-inflation check)
r2 = con.execute("""SELECT COUNT(*) total, SUM(socials_count=0 AND has_twitter=0) both_zero,
                    SUM(is_live=0) retro FROM training_rows WHERE kind='signal'""").fetchone()
print("\nALL kind=signal rows (no filters):", dict(r2))

# token_static social columns
r3 = con.execute("""SELECT COUNT(*) total,
  SUM(twitter IS NULL AND telegram IS NULL AND website IS NULL AND discord IS NULL) all4_null,
  SUM(twitter IS NOT NULL) tw, SUM(telegram IS NOT NULL) tg,
  SUM(website IS NOT NULL) ws, SUM(discord IS NOT NULL) dc,
  SUM(twitter='') tw_empty
  FROM token_static""").fetchone()
print("token_static:", dict(r3))

# does description_len / has_banner behave the same way (project convention)?
r4 = con.execute("""SELECT COUNT(*) n, SUM(description_len=0) d0, SUM(description_len IS NULL) dnull,
  SUM(has_banner=0) b0, SUM(has_banner IS NULL) bnull, SUM(exchanges_count IS NULL) ex_null
  FROM token_static""").fetchone()
print("token_static conventions:", dict(r4))
con.close()
