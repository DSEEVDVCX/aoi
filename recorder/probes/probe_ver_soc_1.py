import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1 AND {FV}"

print("current_feature_version =",
      con.execute("SELECT value FROM meta WHERE key='current_feature_version'").fetchone()[0])

r = con.execute(f"""SELECT COUNT(*) n,
   SUM(socials_count IS NULL) sc_null,
   SUM(socials_count=0) sc0,
   SUM(socials_count>0) scpos,
   SUM(has_twitter IS NULL) tw_null,
   SUM(has_twitter=0) tw0,
   SUM(has_twitter=1) tw1,
   COUNT(DISTINCT token_address||'|'||network_id) toks
  FROM training_rows WHERE {POP}""").fetchone()
print("\n== A. model population, socials columns ==")
print(dict(r))

# cross-tab: socials_count value distribution
print("\n== B. socials_count value distribution (model pop) ==")
for row in con.execute(f"""SELECT socials_count v, COUNT(*) n,
        COUNT(DISTINCT token_address||'|'||network_id) toks
      FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 1"""):
    print("  socials_count =", row["v"], "->", row["n"], "rows,", row["toks"], "tokens")

print("\n== C. has_twitter distribution (model pop) ==")
for row in con.execute(f"""SELECT has_twitter v, COUNT(*) n,
        COUNT(DISTINCT token_address||'|'||network_id) toks
      FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 1"""):
    print("  has_twitter =", row["v"], "->", row["n"], "rows,", row["toks"], "tokens")

# how many rows with socials_count=0 belong to a token whose token_static
# NEVER stored any of the four links, at any snapshot
print("\n== D. rows with socials_count=0 vs token_static link presence (SQL only) ==")
r = con.execute(f"""
WITH pop AS (SELECT token_address a, network_id nw, socials_count sc, has_twitter tw
               FROM training_rows WHERE {POP}),
     ts AS (SELECT token_address a, network_id nw,
                   MAX(twitter IS NOT NULL OR telegram IS NOT NULL
                       OR website IS NOT NULL OR discord IS NOT NULL) any_link,
                   COUNT(*) snaps
              FROM token_static GROUP BY 1,2)
SELECT COUNT(*) rows_sc0,
       SUM(CASE WHEN ts.any_link=0 THEN 1 ELSE 0 END) sc0_token_never_any_link,
       SUM(CASE WHEN ts.any_link=1 THEN 1 ELSE 0 END) sc0_token_has_link_somewhere,
       SUM(CASE WHEN ts.a IS NULL THEN 1 ELSE 0 END) sc0_no_static_row
  FROM pop LEFT JOIN ts ON ts.a=pop.a AND ts.nw=pop.nw
 WHERE pop.sc=0""").fetchone()
print(dict(r))

print("\n== E. token_static census ==")
r = con.execute("""SELECT COUNT(*) rows, COUNT(DISTINCT token_address||'|'||network_id) toks,
   SUM(twitter IS NULL AND telegram IS NULL AND website IS NULL AND discord IS NULL) all4_null,
   SUM(twitter IS NOT NULL) tw,
   SUM(website IS NOT NULL) web,
   SUM(telegram IS NOT NULL) tg,
   SUM(discord IS NOT NULL) dc
   FROM token_static""").fetchone()
print(dict(r))

# does the SAME token flip between "some link" and "no link" across snapshots?
print("\n== F. per-token stability of the stored-link state across snapshots ==")
r = con.execute("""
WITH t AS (SELECT token_address a, network_id nw,
      MIN(CASE WHEN twitter IS NULL AND telegram IS NULL AND website IS NULL
                AND discord IS NULL THEN 0 ELSE 1 END) mn,
      MAX(CASE WHEN twitter IS NULL AND telegram IS NULL AND website IS NULL
                AND discord IS NULL THEN 0 ELSE 1 END) mx,
      COUNT(*) snaps FROM token_static GROUP BY 1,2)
SELECT COUNT(*) tokens, SUM(snaps>1) multi_snap,
       SUM(mn<>mx) flipping_tokens FROM t""").fetchone()
print(dict(r))
con.close()
